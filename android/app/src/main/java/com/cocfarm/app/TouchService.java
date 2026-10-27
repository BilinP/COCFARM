package com.cocfarm.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.view.accessibility.AccessibilityEvent;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * COCFARM's fingers: taps, quick bursts of taps, swipes and pinches in the
 * game, as accessibility gestures (no root needed). Also notes which app is
 * in front. The player turns it on once in Settings → Accessibility.
 */
public class TouchService extends AccessibilityService {
    static volatile TouchService instance;
    private static final long TAP_MS = 40;
    private static final long BURST_MS = 300;
    private volatile String foreground = "";
    FloatingButton button;

    @Override
    protected void onServiceConnected() {
        button = new FloatingButton(this);
        instance = this;
        if (BotService.instance != null) {
            button.show();
        }
    }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        instance = null;
        if (button != null) {
            button.hide();
        }
        return super.onUnbind(intent);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getEventType() != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED || event.getPackageName() == null) {
            return;
        }
        String app = event.getPackageName().toString();
        // The notification shade, the keyboard and COCFARM's own floating button open over the
        // game: it's still the app in front.
        boolean ownOverlay = app.equals(getPackageName())
                && (event.getClassName() == null || !event.getClassName().toString().startsWith(getPackageName()));
        if (!app.equals("com.android.systemui") && !app.contains("inputmethod") && !ownOverlay) {
            foreground = app;
            if (button != null) {
                button.refresh();
            }
        }
    }

    @Override
    public void onInterrupt() {
    }

    String foregroundPackage() {
        return foreground;
    }

    /**
     * Play a gesture and wait until it's done (or failed). Only in the game:
     * with another app in front (COCFARM's own screen, say) the taps would
     * land on that app, so nothing is played. (Not known yet, as when this
     * service has just started: it's taken to be the game.)
     */
    boolean play(GestureDescription gesture, long longestMs) {
        if (!foreground.isEmpty() && !FloatingButton.GAME.equals(foreground)) {
            return false;
        }
        CountDownLatch done = new CountDownLatch(1);
        boolean[] ok = {false};
        // Only a gesture that crosses the floating button needs it out of the way.
        boolean through = button.passThrough(gesture);
        try {
            return play(gesture, longestMs, done, ok);
        } finally {
            if (through) {
                button.endPassThrough();
            }
        }
    }

    private boolean play(GestureDescription gesture, long longestMs, CountDownLatch done, boolean[] ok) {
        boolean sent = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription g) {
                ok[0] = true;
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription g) {
                done.countDown();
            }
        }, null);
        if (!sent) {
            return false;
        }
        try {
            done.await(longestMs + 2000, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return ok[0];
    }

    boolean tap(int x, int y) {
        Path path = new Path();
        path.moveTo(x, y);
        return play(new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, TAP_MS)).build(), TAP_MS);
    }

    /**
     * Tap every point, {@code gapMs} apart: up to the most strokes Android
     * takes in one gesture at a time, each starting a little after the last.
     */
    boolean tapMany(int[] xs, int[] ys, long gapMs) {
        long gap = Math.max(gapMs, TAP_MS + 10);  // one finger lifts before the next lands
        // Short gestures: switching apps mid-burst leaves at most this much to land on the other app.
        int most = (int) Math.max(1, Math.min(GestureDescription.getMaxStrokeCount(), BURST_MS / gap));
        for (int start = 0; start < xs.length; start += most) {
            int end = Math.min(xs.length, start + most);
            GestureDescription.Builder builder = new GestureDescription.Builder();
            for (int i = start; i < end; i++) {
                Path path = new Path();
                path.moveTo(xs[i], ys[i]);
                builder.addStroke(new GestureDescription.StrokeDescription(path, (i - start) * gap, TAP_MS));
            }
            if (!play(builder.build(), (end - start) * gap + TAP_MS)) {
                return false;
            }
        }
        return true;
    }

    boolean swipe(int x1, int y1, int x2, int y2, long ms) {
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        return play(new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, Math.max(ms, 1))).build(), ms);
    }

    /** Two fingers closing in around the middle: zooms the view out. */
    boolean pinch(int width, int height) {
        int cx = width / 2, cy = Math.round(height * 0.45f);
        // The fingers stop well apart: closing them on one spot reads as a tap.
        int far = width / 3, near = width / 8;
        Path left = new Path(), right = new Path();
        left.moveTo(cx - far, cy);
        left.lineTo(cx - near, cy);
        right.moveTo(cx + far, cy);
        right.lineTo(cx + near, cy);
        long ms = 350;
        return play(new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(left, 0, ms))
                .addStroke(new GestureDescription.StrokeDescription(right, 0, ms)).build(), ms);
    }

    boolean home() {
        return performGlobalAction(GLOBAL_ACTION_HOME);
    }

    boolean back() {
        return performGlobalAction(GLOBAL_ACTION_BACK);
    }
}
