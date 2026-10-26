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
import android.view.WindowManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.nio.ByteBuffer;

/**
 * Runs while COCFARM farms: a foreground service (with its notification and
 * a Stop button) that sees the screen through screen capture and runs the
 * bot, in Python, on its own thread.
 */
public class BotService extends Service {
    static final String ACTION_START = "start", ACTION_STOP = "stop";
    static final String EXTRA_RESULT = "result", EXTRA_DATA = "data";
    private static final String CHANNEL = "farming";

    static volatile BotService instance;
    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private HandlerThread captureThread;
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
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            if (botThread != null) {
                // The bot stops at its next safe point; the screen capture stays on until then
                // (the bot thread ends the service when it's done).
                stopBot();
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
            startBot();
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
        Handler handler = new Handler(captureThread.getLooper());
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
        return true;
    }

    /** The newest screen; the last one again if nothing has changed since. */
    Frame frame() throws InterruptedException {
        long deadline = System.currentTimeMillis() + 3000;
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
                if (last != null) {
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
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        PyObject bot = Python.getInstance().getModule("cocfarm_android");
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
        if (display != null) display.release();
        if (projection != null) projection.stop();
        if (reader != null) reader.close();
        if (captureThread != null) captureThread.quitSafely();
        super.onDestroy();
    }

    private Notification notification() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26 && manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(new NotificationChannel(CHANNEL, "Farming", NotificationManager.IMPORTANCE_LOW));
        }
        PendingIntent stop = PendingIntent.getService(this, 0, new Intent(this, BotService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        return builder.setContentTitle("COCFARM is farming")
                .setContentText("It plays Clash of Clans until you stop it.")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentIntent(open)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build())
                .build();
    }
}
