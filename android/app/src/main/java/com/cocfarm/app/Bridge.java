package com.cocfarm.app;

import android.app.ActivityManager;
import android.content.Context;
import android.content.Intent;

import com.chaquo.python.Python;

/**
 * What the Python bot calls (see cocfarm_android.py): the screen, the
 * fingers, and the game app. Static, so Python reaches it without a context.
 */
public final class Bridge {
    public interface Listener {
        void onStatus(String text);
    }

    public interface ProgressListener {
        void onProgress(long done, long size);
    }

    static volatile Listener listener;
    static volatile ProgressListener progress;  // an update being downloaded
    static volatile String lastStatus = "Ready when you are.";
    // Asked for by the player (the floating button, the notification or the home screen): the bot
    // acts on them at its next safe point.
    static volatile boolean paused, stopping;

    private Bridge() {
    }

    private static TouchService touch() {
        TouchService service = TouchService.instance;
        if (service == null) {
            throw new IllegalStateException("COCFARM's accessibility service is off: turn it on in Settings → Accessibility");
        }
        return service;
    }

    private static BotService bot() {
        BotService service = BotService.instance;
        if (service == null) {
            throw new IllegalStateException("screen capture isn't running");
        }
        return service;
    }

    public static BotService.Frame screenshot() throws InterruptedException {
        return bot().frame();
    }

    public static int[] screenSize() {
        BotService service = bot();
        return new int[]{service.width, service.height};
    }

    public static boolean tap(int x, int y) {
        return touch().tap(x, y);
    }

    public static boolean tapMany(int[] xs, int[] ys, long gapMs) {
        return touch().tapMany(xs, ys, gapMs);
    }

    public static boolean swipe(int x1, int y1, int x2, int y2, long ms) {
        return touch().swipe(x1, y1, x2, y2, ms);
    }

    public static boolean pinch() {
        BotService service = bot();
        return touch().pinch(service.width, service.height);
    }

    public static boolean back() {
        return touch().back();
    }

    public static String foregroundPackage() {
        return touch().foregroundPackage();
    }

    public static void startApp(String pkg) {
        Context context = bot();
        Intent launch = context.getPackageManager().getLaunchIntentForPackage(pkg);
        if (launch == null) {
            throw new IllegalStateException(pkg + " isn't installed");
        }
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(launch);
    }

    /** Close the game: to the home screen, then end it in the background (no root can force-stop it). */
    public static void stopApp(String pkg) {
        touch().home();
        try {
            Thread.sleep(1500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        ((ActivityManager) bot().getSystemService(Context.ACTIVITY_SERVICE)).killBackgroundProcesses(pkg);
    }

    /** Python, started on first use (from any thread). */
    static synchronized Python python(Context context) {
        if (!Python.isStarted()) {
            Python.start(new com.chaquo.python.android.AndroidPlatform(context.getApplicationContext()));
        }
        return Python.getInstance();
    }

    /** Pause at the next safe point, or carry on. */
    static void togglePause() {
        BotService service = BotService.instance;
        if (service == null || stopping || !Python.isStarted()) {
            return;
        }
        paused = Python.getInstance().getModule("cocfarm_android").callAttr("toggle_pause").toBoolean();
        if (!paused) {
            status("Carrying on");
        }
        changed();
    }

    static void stop(Context context) {
        if (BotService.instance == null) {
            return;
        }
        stopping = true;
        context.startService(new Intent(context, BotService.class).setAction(BotService.ACTION_STOP));
        changed();
    }

    /** Farming started or ended, or was paused: everything showing it follows. */
    static void changed() {
        BotService service = BotService.instance;
        if (service != null) {
            service.refreshNotification();
        }
        TouchService touch = TouchService.instance;
        if (touch != null && touch.button != null) {
            touch.button.refresh();
        }
        status(lastStatus);
    }

    /** "Sound on match": the phone's notification sound. */
    public static void ding() {
        BotService service = BotService.instance;
        if (service == null) {
            return;
        }
        android.net.Uri sound = android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_NOTIFICATION);
        android.media.Ringtone ringtone = android.media.RingtoneManager.getRingtone(service, sound);
        if (ringtone != null) {
            ringtone.play();
        }
    }

    public static void updateProgress(long done, long size) {
        ProgressListener l = progress;
        if (l != null) {
            l.onProgress(done, size);
        }
    }

    public static void status(String text) {
        lastStatus = text;
        Listener l = listener;
        if (l != null) {
            l.onStatus(text);
        }
    }
}
