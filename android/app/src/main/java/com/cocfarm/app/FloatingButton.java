package com.cocfarm.app;

import android.accessibilityservice.GestureDescription;
import android.animation.ValueAnimator;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.CornerPathEffect;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PathMeasure;
import android.graphics.PixelFormat;
import android.graphics.RectF;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowManager;
import android.view.animation.DecelerateInterpolator;
import android.view.animation.LinearInterpolator;
import android.widget.Toast;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * A small round button over the game while COCFARM farms: tap to pause or
 * carry on, hold to stop (a ring fills while it's held), drag to move it.
 * Drawn by the accessibility service (no "draw over other apps" permission
 * needed); the bot's own taps pass through it.
 */
final class FloatingButton {
    private static final int SIZE_DP = 56;  // the window: the 44 dp button and room for its shadow
    private static final long HOLD_MS = 800;
    private static final long PASS_MS = 40;
    static final String GAME = "com.supercell.clashofclans";  // shown only over the game

    private final TouchService service;
    private final WindowManager windows;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SharedPreferences prefs;
    private Face view;
    private WindowManager.LayoutParams params;
    private volatile boolean botTouching;  // a bot gesture that crosses the button is playing
    private volatile boolean playerTouching;  // the player's finger is on the button: it stays put

    FloatingButton(TouchService service) {
        this.service = service;
        windows = service.getSystemService(WindowManager.class);
        prefs = service.getSharedPreferences("floating_button", Context.MODE_PRIVATE);
    }

    void show() {
        main.post(() -> {
            if (view != null) {
                return;
            }
            int size = dp(SIZE_DP);
            view = new Face(service);
            params = new WindowManager.LayoutParams(size, size, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                    PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.TOP | Gravity.START;
            // Where the player left it; at first, the left edge a little below the middle: map on
            // the village, scouting, battle and results screens, clear of the buttons the bot
            // looks for (it shows in the screenshots).
            DisplayMetrics screen = service.getResources().getDisplayMetrics();
            int width = Math.max(screen.widthPixels, screen.heightPixels);
            int height = Math.min(screen.widthPixels, screen.heightPixels);
            params.x = clamp(prefs.getInt("x", 0), width - size);
            params.y = clamp(prefs.getInt("y", height * 55 / 100), height - size);
            view.setOnTouchListener(new Drag());
            view.setVisibility(overGame() ? View.VISIBLE : View.GONE);
            view.setAlpha(0f);
            windows.addView(view, params);
            view.animate().alpha(1f).setDuration(200).start();
        });
    }

    void hide() {
        main.post(() -> {
            if (view != null) {
                windows.removeView(view);
                view = null;
            }
            playerTouching = botTouching = false;
        });
    }

    /** Show whether it's farming, paused or stopping (see Bridge), and only over the game. */
    void refresh() {
        main.post(() -> {
            if (view != null) {
                view.setVisibility(overGame() ? View.VISIBLE : View.GONE);
                view.invalidate();
            }
        });
    }

    private boolean overGame() {
        return GAME.equals(service.foregroundPackage());
    }

    /**
     * If the bot's gesture crosses the button, let touches through it while
     * the gesture plays (an accessibility gesture lands on whatever window is
     * on top there). True if it did: call endPassThrough() after.
     */
    boolean passThrough(GestureDescription gesture) {
        WindowManager.LayoutParams at = params;
        if (view == null || at == null || view.getVisibility() != View.VISIBLE || playerTouching) {
            return false;
        }
        int margin = dp(8);
        RectF area = new RectF(at.x - margin, at.y - margin, at.x + at.width + margin, at.y + at.height + margin);
        float[] point = new float[2];
        boolean crosses = false;
        for (int i = 0; i < gesture.getStrokeCount() && !crosses; i++) {
            Path stroke = gesture.getStroke(i).getPath();
            PathMeasure measure = new PathMeasure(stroke, false);
            float length = measure.getLength();
            if (length == 0) {  // a tap: just the spot
                RectF spot = new RectF();
                stroke.computeBounds(spot, true);
                crosses = area.contains(spot.left, spot.top);
                continue;
            }
            for (float d = 0; d <= length && !crosses; d += Math.max(1, Math.min(10, length))) {
                measure.getPosTan(d, point, null);
                crosses = area.contains(point[0], point[1]);
            }
        }
        if (crosses) {
            setTouchable(false);
        }
        return crosses;
    }

    void endPassThrough() {
        setTouchable(true);
    }

    private void setTouchable(boolean touchable) {
        boolean through = !touchable;
        botTouching = through;
        if (view == null) {
            return;
        }
        CountDownLatch done = new CountDownLatch(1);
        main.post(() -> {
            if (view != null) {
                if (through) {
                    params.flags |= WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
                } else {
                    params.flags &= ~WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
                }
                windows.updateViewLayout(view, params);
            }
            done.countDown();
        });
        try {
            done.await(500, TimeUnit.MILLISECONDS);
            if (through) {
                Thread.sleep(PASS_MS);  // the window manager applies the change a frame or two later
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private void togglePause() {
        Bridge.togglePause();
        Toast.makeText(service, Bridge.paused ? "COCFARM pauses after what it's doing now" : "COCFARM carries on",
                Toast.LENGTH_SHORT).show();
    }

    /** The button: a dark disc with a coloured ring and the action's icon. */
    private final class Face extends View {
        private final Paint disc = new Paint(Paint.ANTI_ALIAS_FLAG), ring = new Paint(Paint.ANTI_ALIAS_FLAG),
                icon = new Paint(Paint.ANTI_ALIAS_FLAG), hold = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path path = new Path();
        private final RectF box = new RectF();
        private float held;  // 0..1: how far a hold to stop has got
        private ValueAnimator holding;

        Face(Context context) {
            super(context);
            setLayerType(LAYER_TYPE_SOFTWARE, null);  // for the shadow
            disc.setColor(context.getColor(R.color.cf_surface));
            disc.setShadowLayer(dp(4), 0, dp(1), 0x99000000);
            ring.setStyle(Paint.Style.STROKE);
            ring.setStrokeWidth(dp(3));
            hold.setStyle(Paint.Style.STROKE);
            hold.setStrokeWidth(dp(3));
            hold.setStrokeCap(Paint.Cap.ROUND);
            hold.setColor(context.getColor(R.color.cf_bad));
            icon.setPathEffect(new CornerPathEffect(dp(1)));
            setContentDescription("COCFARM: tap to pause or carry on, hold to stop");
        }

        @Override
        protected void onDraw(Canvas canvas) {
            float cx = getWidth() / 2f, cy = getHeight() / 2f, r = dp(22);
            int colour = getContext().getColor(Bridge.stopping ? R.color.cf_bad
                    : Bridge.paused ? R.color.cf_warn : R.color.cf_accent);
            canvas.drawCircle(cx, cy, r, disc);
            ring.setColor(colour);
            canvas.drawCircle(cx, cy, r - dp(1.5f), ring);
            if (held > 0) {
                box.set(cx - r + dp(1.5f), cy - r + dp(1.5f), cx + r - dp(1.5f), cy + r - dp(1.5f));
                canvas.drawArc(box, -90, 360 * held, false, hold);
            }
            icon.setColor(Bridge.paused || Bridge.stopping ? colour : Color.WHITE);
            float s = dp(7);  // half the icon's size
            path.reset();
            if (Bridge.stopping || held > 0) {
                icon.setColor(getContext().getColor(R.color.cf_bad));
                path.addRoundRect(cx - s * 0.8f, cy - s * 0.8f, cx + s * 0.8f, cy + s * 0.8f, dp(2), dp(2), Path.Direction.CW);
            } else if (Bridge.paused) {  // play: carry on
                path.moveTo(cx - s * 0.7f, cy - s);
                path.lineTo(cx + s, cy);
                path.lineTo(cx - s * 0.7f, cy + s);
                path.close();
            } else {  // pause
                float bar = s * 0.62f, gap = s * 0.3f;
                path.addRoundRect(cx - gap - bar, cy - s, cx - gap, cy + s, dp(1.5f), dp(1.5f), Path.Direction.CW);
                path.addRoundRect(cx + gap, cy - s, cx + gap + bar, cy + s, dp(1.5f), dp(1.5f), Path.Direction.CW);
            }
            canvas.drawPath(path, icon);
        }

        void press(boolean down) {
            animate().scaleX(down ? 0.9f : 1f).scaleY(down ? 0.9f : 1f).setDuration(120)
                    .setInterpolator(new DecelerateInterpolator()).start();
        }

        void startHold(Runnable done) {
            holding = ValueAnimator.ofFloat(0f, 1f).setDuration(HOLD_MS);
            holding.setInterpolator(new LinearInterpolator());
            holding.addUpdateListener(a -> {
                held = (float) a.getAnimatedValue();
                invalidate();
                if (held >= 1f) {
                    holding = null;
                    done.run();
                }
            });
            holding.start();
        }

        void cancelHold() {
            if (holding != null) {
                holding.cancel();
                holding = null;
            }
            held = 0;
            invalidate();
        }
    }

    private final class Drag implements View.OnTouchListener {
        private final int slop = ViewConfiguration.get(service).getScaledTouchSlop();
        private float downX, downY;
        private int startX, startY;
        private boolean moved, held;

        @Override
        public boolean onTouch(View v, MotionEvent event) {
            Face face = (Face) v;
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    if (botTouching) {
                        return false;  // the bot's tap, landing before the button let it through
                    }
                    playerTouching = true;
                    downX = event.getRawX();
                    downY = event.getRawY();
                    startX = params.x;
                    startY = params.y;
                    moved = held = false;
                    face.press(true);
                    if (!Bridge.stopping) {
                        face.startHold(() -> {
                            held = true;
                            v.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS);
                            Bridge.stop(service);
                            Toast.makeText(service, "Stopping COCFARM", Toast.LENGTH_SHORT).show();
                        });
                    }
                    return true;
                case MotionEvent.ACTION_MOVE:
                    float dx = event.getRawX() - downX, dy = event.getRawY() - downY;
                    if (!moved && !held && Math.hypot(dx, dy) > slop) {
                        moved = true;
                        face.cancelHold();
                    }
                    if (moved) {
                        DisplayMetrics screen = service.getResources().getDisplayMetrics();
                        params.x = clamp(startX + Math.round(dx), screen.widthPixels - v.getWidth());
                        params.y = clamp(startY + Math.round(dy), screen.heightPixels - v.getHeight());
                        windows.updateViewLayout(v, params);
                    }
                    return true;
                case MotionEvent.ACTION_UP:
                    playerTouching = false;
                    face.press(false);
                    if (!held) {
                        face.cancelHold();
                    }
                    if (moved) {
                        prefs.edit().putInt("x", params.x).putInt("y", params.y).apply();
                    } else if (!held && !Bridge.stopping) {
                        v.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
                        togglePause();
                    }
                    return true;
                case MotionEvent.ACTION_CANCEL:
                    playerTouching = false;
                    face.press(false);
                    if (!held) {
                        face.cancelHold();
                    }
                    return true;
                default:
                    return false;
            }
        }
    }

    private static int clamp(int value, int most) {
        return Math.max(0, Math.min(value, most));
    }

    private int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                service.getResources().getDisplayMetrics()));
    }
}
