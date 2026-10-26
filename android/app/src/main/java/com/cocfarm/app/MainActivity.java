package com.cocfarm.app;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * COCFARM's one screen: the two permissions it needs, Start and Stop, and
 * what the bot is doing. Settings and pictures live in its files folder.
 */
public class MainActivity extends Activity {
    private static final int CAPTURE = 1;
    private TextView access, status, folder;
    private Button start, stop;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        column.setPadding(pad, pad, pad, pad);

        TextView title = text("COCFARM", 26);
        title.setTextColor(Color.parseColor("#4c9aff"));
        column.addView(title);
        column.addView(text("Farms Clash of Clans on this device. Not affiliated with Supercell; botting can get "
                + "your account banned.", 14));

        column.addView(heading("1. Let COCFARM tap in the game"));
        access = text("", 14);
        column.addView(access);
        Button settings = button("Open Accessibility settings");
        settings.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        column.addView(settings);

        column.addView(heading("2. Settings and pictures"));
        folder = text("", 14);
        column.addView(folder);

        column.addView(heading("3. Farm"));
        start = button("Start");
        start.setOnClickListener(v -> askToCapture());
        stop = button("Stop");
        stop.setOnClickListener(v -> startService(new Intent(this, BotService.class).setAction(BotService.ACTION_STOP)));
        column.addView(start);
        column.addView(stop);
        status = text("", 16);
        status.setPadding(0, dp(12), 0, 0);
        column.addView(status);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(column);
        setContentView(scroll);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 0);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        Bridge.listener = text -> runOnUiThread(() -> showStatus(text));
        refresh();
    }

    @Override
    protected void onPause() {
        Bridge.listener = null;
        super.onPause();
    }

    private void refresh() {
        boolean on = accessibilityOn();
        access.setText(on ? "✓ COCFARM can tap in the game." : "Turn on COCFARM under Settings → Accessibility "
                + "(Installed apps / Downloaded apps on some phones).");
        access.setTextColor(on ? Color.parseColor("#6ccb5f") : Color.parseColor("#fce100"));
        folder.setText("Your settings (farmbot.toml) and button pictures (templates) are in:\n"
                + getExternalFilesDir(null).getAbsolutePath());
        boolean running = BotService.instance != null;
        start.setEnabled(!running);
        stop.setEnabled(running);
        showStatus(Bridge.lastStatus);
    }

    private void showStatus(String text) {
        status.setText(text);
        boolean running = BotService.instance != null;
        start.setEnabled(!running);
        stop.setEnabled(running);
    }

    private boolean accessibilityOn() {
        String enabled = Settings.Secure.getString(getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        String mine = new ComponentName(this, TouchService.class).flattenToString();
        if (enabled == null) {
            return false;
        }
        for (String service : TextUtils.split(enabled, ":")) {
            if (service.equalsIgnoreCase(mine)) {
                return true;
            }
        }
        return false;
    }

    private void askToCapture() {
        if (!accessibilityOn()) {
            showStatus("Turn on COCFARM in Accessibility first (step 1).");
            return;
        }
        // Android asks the player each time: COCFARM sees the screen only while it farms.
        MediaProjectionManager manager = getSystemService(MediaProjectionManager.class);
        startActivityForResult(manager.createScreenCaptureIntent(), CAPTURE);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != CAPTURE) {
            return;
        }
        if (result != RESULT_OK || data == null) {
            showStatus("COCFARM needs to see the screen to play: press Start and allow it.");
            return;
        }
        Intent service = new Intent(this, BotService.class).setAction(BotService.ACTION_START)
                .putExtra(BotService.EXTRA_RESULT, result).putExtra(BotService.EXTRA_DATA, data);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(service);
        } else {
            startService(service);
        }
        showStatus("Starting…");
        start.postDelayed(this::refresh, 1500);
    }

    private TextView text(String value, int sp) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        view.setPadding(0, dp(4), 0, dp(4));
        return view;
    }

    private TextView heading(String value) {
        TextView view = text(value, 18);
        view.setPadding(0, dp(18), 0, dp(4));
        return view;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setGravity(Gravity.CENTER);
        return button;
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }
}
