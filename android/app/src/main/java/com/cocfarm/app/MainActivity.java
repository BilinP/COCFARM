package com.cocfarm.app;

import android.Manifest;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.google.android.material.button.MaterialButton;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.snackbar.Snackbar;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * COCFARM's home: what the bot is doing with Start, Pause and Stop; what's
 * left to set up (tapping, notifications, the button pictures); and the way
 * to the settings.
 */
public class MainActivity extends AppCompatActivity {
    private TextView state, status, version;
    private MaterialButton start, pause, stop;
    private View controls, setup;
    private LinearLayout steps;
    private String folder;
    private List<String> missingPictures;  // null until checked
    private ActivityResultLauncher<Intent> capture;
    private ActivityResultLauncher<String> notifications;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.activity_main);
        folder = getExternalFilesDir(null).getAbsolutePath();
        state = findViewById(R.id.state);
        status = findViewById(R.id.status);
        version = findViewById(R.id.version);
        start = findViewById(R.id.start);
        pause = findViewById(R.id.pause);
        stop = findViewById(R.id.stop);
        controls = findViewById(R.id.controls);
        setup = findViewById(R.id.setup);
        steps = findViewById(R.id.steps);
        ((TextView) findViewById(R.id.tip)).setCompoundDrawablesRelative(
                tinted(R.drawable.ic_info, R.color.cf_muted, 18), null, null, null);

        capture = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
            if (result.getResultCode() != RESULT_OK || result.getData() == null) {
                Snackbar.make(start, "COCFARM needs to see the screen to play: press Start and allow it.",
                        Snackbar.LENGTH_LONG).show();
                return;
            }
            Intent service = new Intent(this, BotService.class).setAction(BotService.ACTION_START)
                    .putExtra(BotService.EXTRA_RESULT, result.getResultCode()).putExtra(BotService.EXTRA_DATA, result.getData());
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(service);
            } else {
                startService(service);
            }
            Bridge.status("Starting…");
        });
        notifications = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> refresh());

        start.setOnClickListener(v -> askToCapture());
        pause.setOnClickListener(v -> Bridge.togglePause());
        stop.setOnClickListener(v -> Bridge.stop(this));
        buildLinks();
    }

    @Override
    protected void onResume() {
        super.onResume();
        Bridge.listener = text -> runOnUiThread(this::refresh);
        refresh();
        checkPictures();
    }

    @Override
    protected void onPause() {
        Bridge.listener = null;
        super.onPause();
    }

    // ------------------------------------------------------------- status

    private void refresh() {
        boolean running = BotService.instance != null;
        String label;
        int fore, back;
        if (!running) {
            label = "Not farming";
            fore = R.color.cf_muted;
            back = R.color.cf_surface_highest;
        } else if (Bridge.stopping) {
            label = "Stopping";
            fore = R.color.cf_bad;
            back = R.color.cf_bad_container;
        } else if (Bridge.paused) {
            label = "Paused";
            fore = R.color.cf_warn;
            back = R.color.cf_warn_container;
        } else {
            label = "Farming";
            fore = R.color.cf_good;
            back = R.color.cf_good_container;
        }
        state.setText(label);
        state.setTextColor(getColor(fore));
        GradientDrawable pill = (GradientDrawable) ContextCompat.getDrawable(this, R.drawable.bg_pill).mutate();
        pill.setColor(getColor(back));
        state.setBackground(pill);
        state.setCompoundDrawablesRelative(tinted(R.drawable.dot, fore, 8), null, null, null);
        status.setText(Bridge.lastStatus);

        start.setVisibility(running ? View.GONE : View.VISIBLE);
        controls.setVisibility(running ? View.VISIBLE : View.GONE);
        pause.setText(Bridge.paused ? "Resume" : "Pause");
        pause.setIconResource(Bridge.paused ? R.drawable.ic_play : R.drawable.ic_pause);
        pause.setEnabled(!Bridge.stopping);
        stop.setEnabled(!Bridge.stopping);
        refreshSteps();
    }

    // ------------------------------------------------------------- setup

    private void refreshSteps() {
        steps.removeAllViews();
        boolean tapping = accessibilityOn();
        boolean notifying = Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        boolean pictures = missingPictures != null && missingPictures.isEmpty();
        addStep(tapping, R.drawable.ic_touch, "Let COCFARM tap in the game",
                tapping ? "Turned on." : "Turn on COCFARM under Accessibility (in Installed or Downloaded apps).",
                "Turn on", v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        addStep(notifying, R.drawable.ic_notifications, "Allow notifications",
                notifying ? "Allowed." : "So you can pause and stop it from the notification.",
                "Allow", v -> notifications.launch(Manifest.permission.POST_NOTIFICATIONS));
        String detail = missingPictures == null ? "Checking…" : pictures ? "All there."
                : "Missing: " + TextUtils.join(", ", missingPictures) + ".";
        addStep(pictures, R.drawable.ic_image, "Button pictures", detail, "Where?", v -> showFolder());
        setup.setVisibility(tapping && notifying && pictures ? View.GONE : View.VISIBLE);
    }

    private void addStep(boolean done, int iconRes, String title, String detail, String action, View.OnClickListener act) {
        View row = row(steps, done ? R.drawable.ic_check_circle : iconRes, title, detail);
        ImageView icon = row.findViewById(R.id.icon);
        icon.setImageTintList(ColorStateList.valueOf(getColor(done ? R.color.cf_good : R.color.cf_warn)));
        if (!done) {
            MaterialButton button = row.findViewById(R.id.action);
            button.setText(action);
            button.setVisibility(View.VISIBLE);
            button.setOnClickListener(act);
        }
    }

    private void checkPictures() {
        new Thread(() -> {
            List<String> missing = new ArrayList<>();
            String shown;
            try {
                JSONObject info = new JSONObject(Bridge.python(this).getModule("cocfarm_settings")
                        .callAttr("info", folder).toString());
                JSONArray names = info.getJSONArray("missing");
                for (int i = 0; i < names.length(); i++) {
                    missing.add(names.getString(i));
                }
                shown = "v" + info.getString("version");
            } catch (Exception e) {
                shown = "";
            }
            String v = shown;
            runOnUiThread(() -> {
                missingPictures = missing;
                version.setText(v);
                refresh();
            });
        }, "check-pictures").start();
    }

    private void showFolder() {
        new MaterialAlertDialogBuilder(this)
                .setTitle("Button pictures")
                .setMessage("Put the templates folder (the button pictures) in COCFARM's folder:\n\n" + folder
                        + "\n\nConnect the phone to a PC and copy it there, or use a file manager.")
                .setPositiveButton("Copy the path", (d, w) -> copyFolder())
                .setNegativeButton("Close", null)
                .show();
    }

    private void copyFolder() {
        getSystemService(ClipboardManager.class).setPrimaryClip(ClipData.newPlainText("COCFARM folder", folder));
        Snackbar.make(start, "Copied the folder's path", Snackbar.LENGTH_SHORT).show();
    }

    // ------------------------------------------------------------- links

    private void buildLinks() {
        LinearLayout links = findViewById(R.id.links);
        View settings = row(links, R.drawable.ic_settings, "Settings", "Farming, home village, builder base and when to play");
        settings.findViewById(R.id.chevron).setVisibility(View.VISIBLE);
        settings.setOnClickListener(v -> startActivity(new Intent(this, SettingsActivity.class)));
        // Shown as a file manager shows it: from the phone's storage.
        View files = row(links, R.drawable.ic_folder, "COCFARM's folder",
                folder.replace(android.os.Environment.getExternalStorageDirectory().getAbsolutePath() + "/", ""));
        MaterialButton copy = files.findViewById(R.id.action);
        copy.setVisibility(View.VISIBLE);
        copy.setText("Copy");
        copy.setOnClickListener(v -> copyFolder());
        files.setOnClickListener(v -> showFolder());
    }

    private View row(ViewGroup parent, int iconRes, String title, String detail) {
        View row = LayoutInflater.from(this).inflate(R.layout.row_item, parent, false);
        ImageView icon = row.findViewById(R.id.icon);
        icon.setImageResource(iconRes);
        icon.setImageTintList(ColorStateList.valueOf(getColor(R.color.cf_accent)));
        ((TextView) row.findViewById(R.id.title)).setText(title);
        ((TextView) row.findViewById(R.id.detail)).setText(detail);
        parent.addView(row);
        return row;
    }

    // ------------------------------------------------------------- starting

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
            Snackbar.make(start, "Let COCFARM tap in the game first.", Snackbar.LENGTH_LONG)
                    .setAction("Turn on", v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))).show();
            return;
        }
        if (missingPictures != null && !missingPictures.isEmpty()) {
            Snackbar.make(start, "Add the button pictures first.", Snackbar.LENGTH_LONG)
                    .setAction("Where?", v -> showFolder()).show();
            return;
        }
        // Android asks the player each time: COCFARM sees the screen only while it farms.
        capture.launch(getSystemService(MediaProjectionManager.class).createScreenCaptureIntent());
    }

    private Drawable tinted(int res, int colour, int sizeDp) {
        Drawable icon = ContextCompat.getDrawable(this, res).mutate();
        int size = Math.round(sizeDp * getResources().getDisplayMetrics().density);
        icon.setBounds(0, 0, size, size);
        icon.setTint(getColor(colour));
        return icon;
    }
}
