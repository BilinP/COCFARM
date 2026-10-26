package com.cocfarm.app;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.WindowManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.nio.ByteBuffer;

/**
 * Runs while COCFARM farms: a foreground service (with its notification and
 * a Stop button) that sees the screen through screen capture and runs the
 * bot, in Python, on its own thread.
 */
public class BotService extends Service {
    static final String ACTION_START = "start", ACTION_STOP = "stop", ACTION_PAUSE = "pause";
    static final String EXTRA_RESULT = "result", EXTRA_DATA = "data";
    private static final String CHANNEL = "farming";
    // Between rounds and outside play hours the bot doesn't look at the screen: after this long
    // without a screenshot the capture stops drawing (saving battery) until the next one.
    private static final long IDLE_MS = 8000;

    static volatile BotService instance;
    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private HandlerThread captureThread;
    private Handler handler;
    private long lastAsked;
    private boolean paused;
    private Thread botThread;
    private final Object frameLock = new Object();
    private Frame last;
    int width, height;

    /** A screenshot: RGBA pixels, {@code rowStride} bytes a row. */
    public static class Frame {
        public final int width, height, rowStride;
        public final byte[] data;

        Frame(int width, int height, int rowStride, byte[] data) {
            this.width = width;
            this.height = height;
            this.rowStride = rowStride;
            this.data = data;
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_PAUSE.equals(intent.getAction())) {
            Bridge.togglePause();
            return START_NOT_STICKY;
        }
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            Bridge.stopping = botThread != null;
            if (botThread != null) {
                // The bot stops at its next safe point; the screen capture stays on until then
                // (the bot thread ends the service when it's done).
                stopBot();
                Bridge.changed();
            } else {
                stopSelf();
            }
            return START_NOT_STICKY;
        }
        if (ACTION_START.equals(intent.getAction()) && botThread == null) {
            // Android 14: the service must be in the foreground before the screen capture starts.
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(1, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
            } else {
                startForeground(1, notification());
            }
            int result = intent.getIntExtra(EXTRA_RESULT, Activity.RESULT_CANCELED);
            Intent data = Build.VERSION.SDK_INT >= 33 ? intent.getParcelableExtra(EXTRA_DATA, Intent.class)
                    : intent.getParcelableExtra(EXTRA_DATA);
            if (!startCapture(result, data)) {
                Bridge.status("Screen capture wasn't allowed: press Start and allow it.");
                stopSelf();
                return START_NOT_STICKY;
            }
            instance = this;
            Bridge.paused = Bridge.stopping = false;
            TouchService touch = TouchService.instance;
            if (touch != null) {
                touch.button.show();
            }
            startBot();
            Bridge.changed();
        }
        return START_NOT_STICKY;
    }

    private boolean startCapture(int result, Intent data) {
        if (result != Activity.RESULT_OK || data == null) {
            return false;
        }
        MediaProjectionManager manager = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        projection = manager.getMediaProjection(result, data);
        if (projection == null) {
            return false;
        }
        captureThread = new HandlerThread("capture");
        captureThread.start();
        handler = new Handler(captureThread.getLooper());
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                if (botThread != null) {  // ended from outside (e.g. the system's cast tile), not by Stop
                    Bridge.status("Screen capture stopped.");
                    stopBot();
                }
            }
        }, handler);
        DisplayMetrics metrics = new DisplayMetrics();
        ((WindowManager) getSystemService(WINDOW_SERVICE)).getDefaultDisplay().getRealMetrics(metrics);
        // The game is played sideways: capture the screen as it is then.
        width = Math.max(metrics.widthPixels, metrics.heightPixels);
        height = Math.min(metrics.widthPixels, metrics.heightPixels);
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        display = projection.createVirtualDisplay("COCFARM", width, height, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, handler);
        lastAsked = System.currentTimeMillis();
        handler.postDelayed(this::pauseIfIdle, IDLE_MS);
        return true;
    }

    private void pauseIfIdle() {
        synchronized (frameLock) {
            if (!paused && System.currentTimeMillis() - lastAsked >= IDLE_MS) {
                display.setSurface(null);  // the permission stays: no need to ask the player again
                paused = true;
                Log.i("COCFARM", "screen capture paused while idle");
            }
        }
        handler.postDelayed(this::pauseIfIdle, IDLE_MS / 2);
    }

    /** The newest screen; the last one again if nothing has changed since. */
    Frame frame() throws InterruptedException {
        long now = System.currentTimeMillis();
        long deadline = now + 3000;
        // Just woken up: wait a moment for a picture drawn since (the game may not have moved, and
        // then the last one is still right).
        long fresh = 0;
        synchronized (frameLock) {
            lastAsked = now;
            if (paused) {
                Image old = reader.acquireLatestImage();
                if (old != null) old.close();
                display.setSurface(reader.getSurface());
                paused = false;
                Log.i("COCFARM", "screen capture resumed");
                fresh = now + 1000;
            }
        }
        while (true) {
            synchronized (frameLock) {
                Image image = reader.acquireLatestImage();
                if (image != null) {
                    try {
                        Image.Plane plane = image.getPlanes()[0];
                        ByteBuffer buffer = plane.getBuffer();
                        byte[] data = last != null && last.data.length == buffer.remaining() ? last.data
                                : new byte[buffer.remaining()];
                        buffer.get(data);
                        last = new Frame(image.getWidth(), image.getHeight(), plane.getRowStride(), data);
                    } finally {
                        image.close();
                    }
                }
                if (last != null && (image != null || System.currentTimeMillis() >= fresh)) {
                    return last;
                }
            }
            if (System.currentTimeMillis() > deadline) {
                throw new IllegalStateException("no picture from the screen capture");
            }
            Thread.sleep(50);
        }
    }

    private void startBot() {
        PyObject bot = Bridge.python(this).getModule("cocfarm_android");
        String folder = getExternalFilesDir(null).getAbsolutePath();
        botThread = new Thread(() -> {
            try {
                bot.callAttr("run", folder);
            } catch (Throwable e) {
                Bridge.status("Stopped by an error: " + e.getMessage());
            } finally {
                botThread = null;
                stopSelf();
            }
        }, "bot");
        botThread.start();
    }

    private void stopBot() {
        if (botThread != null && Python.isStarted()) {
            Python.getInstance().getModule("cocfarm_android").callAttr("stop");
        }
    }

    @Override
    public void onDestroy() {
        stopBot();
        instance = null;
        Bridge.paused = Bridge.stopping = false;
        TouchService touch = TouchService.instance;
        if (touch != null) {
            touch.button.hide();
        }
        Bridge.changed();  // the screen's Start and Stop buttons follow the service
        if (handler != null) handler.removeCallbacksAndMessages(null);
        if (display != null) display.release();
        if (projection != null) projection.stop();
        if (reader != null) reader.close();
        if (captureThread != null) captureThread.quitSafely();
        super.onDestroy();
    }

    /** The notification again, for a pause or a stop. */
    void refreshNotification() {
        getSystemService(NotificationManager.class).notify(1, notification());
    }

    private Notification notification() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26 && manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(new NotificationChannel(CHANNEL, "Farming", NotificationManager.IMPORTANCE_LOW));
        }
        PendingIntent stop = PendingIntent.getService(this, 0, new Intent(this, BotService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE);
        PendingIntent pause = PendingIntent.getService(this, 1, new Intent(this, BotService.class).setAction(ACTION_PAUSE),
                PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        String title = Bridge.stopping ? "COCFARM is stopping" : Bridge.paused ? "COCFARM is paused" : "COCFARM is farming";
        builder.setContentTitle(title)
                .setContentText(Bridge.paused ? "Resume to carry on farming." : "It plays Clash of Clans until you stop it.")
                .setSmallIcon(R.drawable.ic_bolt)
                .setColor(getColor(Bridge.paused ? R.color.cf_warn : R.color.cf_accent))
                .setContentIntent(open)
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (!Bridge.stopping) {
            builder.addAction(new Notification.Action.Builder(null, Bridge.paused ? "Resume" : "Pause", pause).build())
                    .addAction(new Notification.Action.Builder(null, "Stop", stop).build());
        }
        return builder.build();
    }
}
