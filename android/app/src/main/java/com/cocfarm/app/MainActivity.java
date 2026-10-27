package com.cocfarm.app;

import android.Manifest;
import android.animation.Animator;
import android.animation.AnimatorListenerAdapter;
import android.animation.AnimatorSet;
import android.animation.ObjectAnimator;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.view.animation.AccelerateInterpolator;
import android.view.animation.DecelerateInterpolator;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.core.splashscreen.SplashScreen;
import androidx.core.splashscreen.SplashScreenViewProvider;

import com.google.android.material.button.MaterialButton;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.progressindicator.LinearProgressIndicator;
import com.google.android.material.snackbar.Snackbar;
import com.google.android.material.tabs.TabLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * COCFARM's one screen: the logo and tabs along the top. The Dashboard tab
 * is the Windows app's Dashboard: what the bot is doing with
 * Start, Pause and Stop; a problem that stopped it, with a next step; a
 * newer COCFARM; what this run has done (its numbers, the last base scouted,
 * the builder base); what's left to set up. The other tabs are the settings,
 * page by page (SettingsForm), the History (HistoryPage) and the log (LogPage).
 */
public class MainActivity extends AppCompatActivity {
    private static final long POLL_MS = 1000;
    private static final long LAUNCH_MS = 3000;  // the launch screen shows this long at most
    private static final String[] RESOURCES = {"gold", "elixir", "dark"};
    private static final int[] RESOURCE_COLOURS = {R.color.cf_gold, R.color.cf_elixir, R.color.cf_dark};
    private static final String[] RESOURCE_NAMES = {"Gold", "Elixir", "Dark elixir"};

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService python = Executors.newSingleThreadExecutor();  // one Python call at a time
    private TextView state, status, detail, version, lastAttack, builderLast;
    private MaterialButton start, pause, stop;
    private View controls, setup, session, base, builder, problemCard, updateCard;
    private LinearLayout steps;
    private ImageView basePicture;
    private TextView verdict;
    private View[] counts, gains, loot, builderTiles;
    private String folder;
    private List<String> missingPictures;  // null until checked
    private JSONObject snapshot = new JSONObject();
    private long pictureStamp;
    private boolean polling, asking;
    private volatile boolean ready;  // Python has started: the launch screen can go
    private String updateVersion, updatePage, pendingInstall, stepsShown;
    private TabLayout tabs;
    private FrameLayout pages;
    private View dashboard;
    private TextView saved;
    private SettingsForm settings;
    private HistoryPage history;
    private LogPage log;
    private String shownTab = "dashboard";
    private ActivityResultLauncher<Intent> capture;
    private ActivityResultLauncher<String> notifications;
    private final Runnable poll = new Runnable() {
        @Override
        public void run() {
            askSnapshot();
            if (polling) {
                main.postDelayed(this, POLL_MS);
            }
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        // The launch screen (the logo on the dark background) stays until Python has started, or
        // LAUNCH_MS at most; then the logo grows and fades, and the Dashboard rises into place.
        SplashScreen splash = SplashScreen.installSplashScreen(this);
        long shown = SystemClock.uptimeMillis();
        splash.setKeepOnScreenCondition(() -> !ready && SystemClock.uptimeMillis() - shown < LAUNCH_MS);
        splash.setOnExitAnimationListener(this::leaveSplash);
        super.onCreate(saved);
        setContentView(R.layout.activity_main);
        folder = getExternalFilesDir(null).getAbsolutePath();
        state = findViewById(R.id.state);
        status = findViewById(R.id.status);
        detail = findViewById(R.id.detail);
        version = findViewById(R.id.version);
        start = findViewById(R.id.start);
        pause = findViewById(R.id.pause);
        stop = findViewById(R.id.stop);
        controls = findViewById(R.id.controls);
        setup = findViewById(R.id.setup);
        steps = findViewById(R.id.steps);
        session = findViewById(R.id.session);
        base = findViewById(R.id.base);
        builder = findViewById(R.id.builder);
        problemCard = findViewById(R.id.problem);
        updateCard = findViewById(R.id.update);
        basePicture = findViewById(R.id.base_picture);
        verdict = findViewById(R.id.verdict);
        lastAttack = findViewById(R.id.last_attack);
        builderLast = findViewById(R.id.builder_last);
        ((TextView) findViewById(R.id.tip)).setCompoundDrawablesRelative(
                tinted(R.drawable.ic_info, R.color.cf_muted, 18), null, null, null);
        ((TextView) findViewById(R.id.problem_title)).setCompoundDrawablesRelative(
                tinted(R.drawable.ic_warning, R.color.cf_bad, 22), null, null, null);

        counts = tiles(findViewById(R.id.counts), "Bases looked at", "Attacks", "Running for");
        gains = tiles(findViewById(R.id.gains), "Gold gained", "Elixir gained", "Dark gained");
        loot = tiles(findViewById(R.id.loot), RESOURCE_NAMES);
        builderTiles = tiles(findViewById(R.id.builder_tiles), "Battles", "Gold", "Elixir");
        for (int i = 0; i < 3; i++) {
            label(gains[i]).setCompoundDrawablesRelative(tinted(R.drawable.dot, RESOURCE_COLOURS[i], 8), null, null, null);
            label(loot[i]).setCompoundDrawablesRelative(tinted(R.drawable.dot, RESOURCE_COLOURS[i], 8), null, null, null);
        }

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
        findViewById(R.id.base_card).setOnClickListener(v -> startActivity(new Intent(this, SeenActivity.class)));
        findViewById(R.id.problem_more).setOnClickListener(v -> {
            View details = findViewById(R.id.problem_details);
            details.setVisibility(details.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE);
        });
        findViewById(R.id.update_get).setOnClickListener(v -> downloadUpdate());
        findViewById(R.id.update_notes).setOnClickListener(v -> {
            if (updatePage != null && !updatePage.isEmpty()) {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(updatePage)));
            }
        });
        buildLinks();
        buildTabs();
    }

    // ------------------------------------------------------------- launch

    private void leaveSplash(SplashScreenViewProvider splash) {
        View icon = splash.getIconView();
        View screen = splash.getView();
        AnimatorSet out = new AnimatorSet();
        out.playTogether(
                ObjectAnimator.ofFloat(icon, View.SCALE_X, 1f, 1.5f),
                ObjectAnimator.ofFloat(icon, View.SCALE_Y, 1f, 1.5f),
                ObjectAnimator.ofFloat(icon, View.ALPHA, 1f, 0f),
                ObjectAnimator.ofFloat(screen, View.ALPHA, 1f, 0f));
        out.setDuration(420);
        out.setInterpolator(new AccelerateInterpolator(1.5f));
        out.addListener(new AnimatorListenerAdapter() {
            @Override
            public void onAnimationEnd(Animator animation) {
                splash.remove();
            }
        });
        out.start();
        rise();
    }

    /** The Dashboard's cards float up into place, one after another. */
    private void rise() {
        ViewGroup column = findViewById(R.id.column);
        float lift = getResources().getDisplayMetrics().density * 28;
        int shown = 0;
        for (int i = 0; i < column.getChildCount(); i++) {
            View child = column.getChildAt(i);
            if (child.getVisibility() != View.VISIBLE) continue;
            child.setAlpha(0f);
            child.setTranslationY(lift);
            child.animate().alpha(1f).translationY(0f).setStartDelay(120 + 70L * shown++).setDuration(380)
                    .setInterpolator(new DecelerateInterpolator(2f)).start();
        }
        View bar = findViewById(R.id.tabs);
        bar.setAlpha(0f);
        bar.animate().alpha(1f).setStartDelay(150).setDuration(400).start();
    }

    // ------------------------------------------------------------- tabs

    private void buildTabs() {
        tabs = findViewById(R.id.tabs);
        pages = findViewById(R.id.pages);
        dashboard = findViewById(R.id.dashboard);
        saved = findViewById(R.id.saved);
        tabs.addTab(tabs.newTab().setText("Dashboard").setTag("dashboard"));
        tabs.addTab(tabs.newTab().setText("History").setTag("history"));
        tabs.addTab(tabs.newTab().setText("Log").setTag("log"));
        tabs.addOnTabSelectedListener(new TabLayout.OnTabSelectedListener() {
            public void onTabSelected(TabLayout.Tab tab) {
                show((String) tab.getTag());
            }

            public void onTabUnselected(TabLayout.Tab tab) {
            }

            public void onTabReselected(TabLayout.Tab tab) {
            }
        });
        // The settings pages, after the Dashboard, once Python has read them.
        settings = new SettingsForm(this, folder, saved);
        python.execute(() -> {
            try {
                settings.load();
            } catch (Exception e) {
                return;
            }
            main.post(() -> {
                for (int i = 0; i < settings.pageCount(); i++) {
                    tabs.addTab(tabs.newTab().setText(settings.title(i)).setTag("settings:" + i), 1 + i, false);
                }
            });
        });
        getOnBackPressedDispatcher().addCallback(this, new androidx.activity.OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (!shownTab.equals("dashboard")) {  // back: to the Dashboard first
                    tabs.selectTab(tabs.getTabAt(0));
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                    setEnabled(true);
                }
            }
        });
    }

    private void show(String tab) {
        leave(shownTab);
        shownTab = tab;
        for (int i = pages.getChildCount() - 1; i >= 0; i--) {
            if (pages.getChildAt(i) != dashboard) pages.removeViewAt(i);
        }
        dashboard.setVisibility(tab.equals("dashboard") ? View.VISIBLE : View.GONE);
        saved.setVisibility(View.GONE);
        if (tab.startsWith("settings:")) {
            pages.addView(settings.page(Integer.parseInt(tab.substring("settings:".length()))));
        } else if (tab.equals("history")) {
            if (history == null) history = new HistoryPage(this, folder);
            pages.addView(history.view);
            history.refresh();
        } else if (tab.equals("log")) {
            if (log == null) log = new LogPage(this, folder);
            pages.addView(log.view);
            log.start();
        }
    }

    private void selectTab(String tag) {
        for (int i = 0; i < tabs.getTabCount(); i++) {
            if (tag.equals(tabs.getTabAt(i).getTag())) tabs.selectTab(tabs.getTabAt(i));
        }
    }

    private void leave(String tab) {
        if (tab.startsWith("settings:")) {
            settings.flush();
        } else if (tab.equals("log") && log != null) {
            log.stop();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        Bridge.listener = text -> runOnUiThread(this::refresh);
        polling = true;
        main.post(poll);
        refresh();
        checkPictures();
        checkUpdate();
        if (pendingInstall != null && canInstall()) {
            install(pendingInstall);
        }
        if (shownTab.equals("log") && log != null) {
            log.start();
        }
    }

    @Override
    protected void onPause() {
        if (tabs != null) leave(shownTab);
        Bridge.listener = null;
        polling = false;
        main.removeCallbacks(poll);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        python.shutdown();
        super.onDestroy();
    }

    // ------------------------------------------------------------- status

    private void askSnapshot() {
        if (asking) {
            return;
        }
        asking = true;
        python.execute(() -> {
            JSONObject got = null;
            try {
                got = new JSONObject(Bridge.python(this).getModule("cocfarm_android").callAttr("snapshot", folder).toString());
            } catch (Exception ignored) {
                // Python still starting: next time
            }
            JSONObject shown = got;
            main.post(() -> {
                asking = false;
                if (shown != null) {
                    snapshot = shown;
                    refresh();
                }
            });
        });
    }

    private void refresh() {
        boolean running = BotService.instance != null;
        String botState = snapshot.optString("state", "");
        JSONObject problem = snapshot.optJSONObject("problem");
        String label;
        int fore, back;
        if (!running) {
            boolean failed = problem != null && "problem".equals(botState);
            label = failed ? "Problem" : "Not farming";
            fore = failed ? R.color.cf_bad : R.color.cf_muted;
            back = failed ? R.color.cf_bad_container : R.color.cf_surface_highest;
        } else if (Bridge.stopping) {
            label = "Stopping";
            fore = R.color.cf_bad;
            back = R.color.cf_bad_container;
        } else if (Bridge.paused) {
            label = "Paused";
            fore = R.color.cf_warn;
            back = R.color.cf_warn_container;
        } else if ("attacking".equals(botState)) {
            label = "Attacking";
            fore = R.color.cf_attack;
            back = R.color.cf_attack_container;
        } else if ("waiting".equals(botState)) {
            label = "Waiting";
            fore = R.color.cf_accent;
            back = R.color.cf_accent_container;
        } else {
            label = "Farming";
            fore = R.color.cf_good;
            back = R.color.cf_good_container;
        }
        if (pill(state, label, fore, back)) {
            state.setCompoundDrawablesRelative(tinted(R.drawable.dot, fore, 8), null, null, null);
        }
        boolean live = running && !Bridge.stopping && !Bridge.paused;
        String headline = live && !snapshot.optString("headline").isEmpty() ? snapshot.optString("headline") : Bridge.lastStatus;
        show(status, headline);
        String why = live ? snapshot.optString("detail") : "";
        show(detail, why);
        detail.setVisibility(why.isEmpty() ? View.GONE : View.VISIBLE);

        start.setVisibility(running ? View.GONE : View.VISIBLE);
        controls.setVisibility(running ? View.VISIBLE : View.GONE);
        pause.setText(Bridge.paused ? "Resume" : "Pause");
        pause.setIconResource(Bridge.paused ? R.drawable.ic_play : R.drawable.ic_pause);
        pause.setEnabled(!Bridge.stopping);
        stop.setEnabled(!Bridge.stopping);
        showProblem(running ? null : problem);
        showSession();
        refreshSteps();
    }

    private void showProblem(JSONObject problem) {
        problemCard.setVisibility(problem == null ? View.GONE : View.VISIBLE);
        if (problem == null) {
            return;
        }
        ((TextView) findViewById(R.id.problem_title)).setText(problem.optString("title"));
        ((TextView) findViewById(R.id.problem_why)).setText(problem.optString("why"));
        ((TextView) findViewById(R.id.problem_details)).setText(problem.optString("details"));
        MaterialButton fix = findViewById(R.id.problem_fix);
        switch (problem.optString("action")) {
            case "pictures":
                fix.setText("Where do they go?");
                fix.setOnClickListener(v -> showFolder());
                break;
            case "logs":
                fix.setText("Open the log");
                fix.setOnClickListener(v -> selectTab("log"));
                break;
            default:  // retry, and the Windows-only ones
                fix.setText("Try again");
                fix.setOnClickListener(v -> askToCapture());
        }
    }

    private void showSession() {
        if (snapshot.optDouble("started", 0) == 0) {
            session.setVisibility(View.GONE);
            return;
        }
        session.setVisibility(View.VISIBLE);
        JSONObject today = snapshot.optJSONObject("today");
        JSONArray todayGained = today == null ? null : today.optJSONArray("gained");
        set(counts[0], Fmt.whole(snapshot.optLong("bases")), "");
        set(counts[1], Fmt.whole(snapshot.optLong("attacks")), today == null ? "" : "Today: " + today.optInt("attacks"));
        set(counts[2], Fmt.duration(snapshot.optLong("seconds")), BotService.instance == null ? "Last run" : "");
        JSONArray gained = snapshot.optJSONArray("gained");
        for (int i = 0; i < 3; i++) {
            long value = gained == null ? 0 : gained.optLong(i);
            set(gains[i], Fmt.gain(value), todayGained == null ? "" : "Today " + Fmt.gain(todayGained.optLong(i)));
        }
        JSONObject attack = snapshot.optJSONObject("attack");
        lastAttack.setVisibility(attack == null ? View.GONE : View.VISIBLE);
        if (attack != null) {
            JSONArray target = attack.optJSONArray("target"), got = attack.optJSONArray("gained");
            StringBuilder text = new StringBuilder("Last attack: offered ")
                    .append(Fmt.shortNumber(target.optLong(0))).append(" gold · ")
                    .append(Fmt.shortNumber(target.optLong(1))).append(" elixir · ")
                    .append(Fmt.shortNumber(target.optLong(2))).append(" dark");
            if (!attack.isNull("taken")) {
                text.append(", took ").append(Math.round(attack.optDouble("taken") * 100)).append("%");
            }
            text.append(got == null ? ". Gain unknown: your resources couldn't be read afterwards."
                    : ". Gained " + Fmt.gain(got.optLong(0)) + " · " + Fmt.gain(got.optLong(1)) + " · " + Fmt.gain(got.optLong(2)) + ".");
            show(lastAttack, text);
        }
        showBase(snapshot.optJSONObject("base"));
        showBuilder(snapshot.optJSONObject("builder"));
    }

    private void showBase(JSONObject seen) {
        base.setVisibility(seen == null ? View.GONE : View.VISIBLE);
        if (seen == null) {
            return;
        }
        boolean accepted = seen.optBoolean("accepted");
        pill(verdict, accepted ? "ATTACK" : "SKIP", accepted ? R.color.cf_background : R.color.cf_text,
                accepted ? R.color.cf_good : R.color.cf_surface_highest);
        JSONArray minimums = seen.optJSONArray("minimums");
        for (int i = 0; i < 3; i++) {
            long value = seen.optLong(RESOURCES[i]), minimum = minimums == null ? 0 : minimums.optLong(i);
            boolean met = value >= minimum;
            set(loot[i], Fmt.whole(value), (met ? "✓ " : "✗ ") + "min " + Fmt.shortNumber(minimum));
            note(loot[i]).setTextColor(getColor(met ? R.color.cf_good : R.color.cf_muted));
        }
        long stamp = Math.round(seen.optDouble("time") * 1000);
        if (stamp != pictureStamp) {
            pictureStamp = stamp;
            File picture = new File(folder, "cache/base.jpg");
            python.execute(() -> {  // off the main thread
                Bitmap bitmap = BitmapFactory.decodeFile(picture.getAbsolutePath());
                main.post(() -> {
                    if (bitmap != null) basePicture.setImageBitmap(bitmap);
                });
            });
        }
    }

    private void showBuilder(JSONObject b) {
        int battles = b == null ? 0 : b.optInt("battles");
        builder.setVisibility(battles == 0 ? View.GONE : View.VISIBLE);
        if (battles == 0) {
            return;
        }
        set(builderTiles[0], Fmt.whole(battles), "Second base " + b.optInt("second") + "×");
        set(builderTiles[1], Fmt.gain(b.optLong("gold")), "This session");
        set(builderTiles[2], Fmt.gain(b.optLong("elixir")), "This session");
        JSONObject last = b.optJSONObject("last");
        if (last != null) {
            JSONArray got = last.optJSONArray("gained");
            builderLast.setText("Last battle: " + (last.optInt("stages") >= 2 ? "both bases" : "first base only")
                    + " in " + Fmt.duration(last.optLong("seconds"))
                    + (got == null ? "" : ", gained " + Fmt.gain(got.optLong(0)) + " gold · " + Fmt.gain(got.optLong(1)) + " elixir"));
        }
    }

    // ------------------------------------------------------------- updates

    private void checkUpdate() {
        python.execute(() -> {
            JSONObject found = null;
            try {
                found = new JSONObject(Bridge.python(this).getModule("cocfarm_settings")
                        .callAttr("check_update", folder, false).toString());
            } catch (Exception ignored) {
                // offline, or Python not ready: next time
            }
            JSONObject shown = found;
            main.post(() -> {
                if (shown != null && !shown.isNull("version")) {
                    updateVersion = shown.optString("version");
                    updatePage = shown.optString("page");
                    ((TextView) findViewById(R.id.update_title)).setText("COCFARM " + updateVersion + " is out");
                    updateCard.setVisibility(View.VISIBLE);
                }
            });
        });
    }

    private void downloadUpdate() {
        if (BotService.instance != null) {
            Snackbar.make(start, "Stop farming first: the update restarts COCFARM.", Snackbar.LENGTH_LONG).show();
            return;
        }
        LinearProgressIndicator bar = findViewById(R.id.update_progress);
        MaterialButton get = findViewById(R.id.update_get);
        TextView note = findViewById(R.id.update_note);
        get.setEnabled(false);
        bar.setVisibility(View.VISIBLE);
        bar.setIndeterminate(true);
        note.setText("Downloading…");
        Bridge.progress = (done, size) -> main.post(() -> {
            if (size > 0) {
                bar.setIndeterminate(false);
                bar.setProgressCompat((int) (done * 100 / size), true);
                note.setText("Downloading… " + done / 1048576 + " of " + size / 1048576 + " MB");
            }
        });
        python.execute(() -> {
            String path = null, problem = null;
            try {
                path = Bridge.python(this).getModule("cocfarm_settings").callAttr("download_update", folder).toString();
            } catch (Exception e) {
                problem = e.getMessage();
            }
            String apk = path, why = problem;
            main.post(() -> {
                Bridge.progress = null;
                bar.setVisibility(View.GONE);
                get.setEnabled(true);
                if (apk == null) {
                    note.setText("The download didn't work: " + why);
                    return;
                }
                note.setText("Downloaded. Android asks you to confirm the update.");
                get.setText("Install");
                get.setOnClickListener(v -> install(apk));
                install(apk);
            });
        });
    }

    private boolean canInstall() {
        return Build.VERSION.SDK_INT < 26 || getPackageManager().canRequestPackageInstalls();
    }

    private void install(String path) {
        if (!canInstall()) {  // Android asks the player to let COCFARM install apps, once
            pendingInstall = path;
            startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName())));
            return;
        }
        pendingInstall = null;
        Uri apk = FileProvider.getUriForFile(this, "com.cocfarm.app.files", new File(path));
        startActivity(new Intent(Intent.ACTION_VIEW).setDataAndType(apk, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    // ------------------------------------------------------------- setup

    private void refreshSteps() {
        boolean tapping = accessibilityOn();
        boolean notifying = Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        boolean pictures = missingPictures != null && missingPictures.isEmpty();
        String now = tapping + " " + notifying + " " + missingPictures;
        if (now.equals(stepsShown)) {  // the home screen refreshes every second: rebuild only on a change
            return;
        }
        stepsShown = now;
        steps.removeAllViews();
        addStep(tapping, R.drawable.ic_touch, "Let COCFARM tap in the game",
                tapping ? "Turned on." : "Turn on COCFARM under Accessibility (in Installed or Downloaded apps).",
                "Turn on", v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        addStep(notifying, R.drawable.ic_notifications, "Allow notifications",
                notifying ? "Allowed." : "So you can pause and stop it from the notification.",
                "Allow", v -> notifications.launch(Manifest.permission.POST_NOTIFICATIONS));
        String text = missingPictures == null ? "Checking…" : pictures ? "All there."
                : "Missing: " + TextUtils.join(", ", missingPictures) + ".";
        addStep(pictures, R.drawable.ic_image, "Button pictures", text, "Where?", v -> showFolder());
        setup.setVisibility(tapping && notifying && pictures ? View.GONE : View.VISIBLE);
    }

    private void addStep(boolean done, int iconRes, String title, String text, String action, View.OnClickListener act) {
        View row = row(steps, done ? R.drawable.ic_check_circle : iconRes, title, text);
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
        python.execute(() -> {
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
            ready = true;
            main.post(() -> {
                missingPictures = missing;
                version.setText(v);
                refresh();
            });
        });
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
        // Shown as a file manager shows it: from the phone's storage.
        View files = row(links, R.drawable.ic_folder, "COCFARM's folder",
                folder.replace(Environment.getExternalStorageDirectory().getAbsolutePath() + "/", ""));
        MaterialButton copy = files.findViewById(R.id.action);
        copy.setVisibility(View.VISIBLE);
        copy.setText("Copy");
        copy.setOnClickListener(v -> copyFolder());
        files.setOnClickListener(v -> showFolder());
    }

    private View row(ViewGroup parent, int iconRes, String title, String text) {
        View row = LayoutInflater.from(this).inflate(R.layout.row_item, parent, false);
        ImageView icon = row.findViewById(R.id.icon);
        icon.setImageResource(iconRes);
        icon.setImageTintList(ColorStateList.valueOf(getColor(R.color.cf_accent)));
        ((TextView) row.findViewById(R.id.title)).setText(title);
        ((TextView) row.findViewById(R.id.detail)).setText(text);
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

    // ------------------------------------------------------------- helpers

    /** Tiles side by side in ``row``, one per label. */
    private View[] tiles(LinearLayout row, String... labels) {
        View[] out = new View[labels.length];
        for (int i = 0; i < labels.length; i++) {
            View tile = LayoutInflater.from(this).inflate(R.layout.tile_stat, row, false);
            label(tile).setText(labels[i]);
            row.addView(tile, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
            out[i] = tile;
        }
        return out;
    }

    private static TextView label(View tile) {
        return tile.findViewById(R.id.label);
    }

    private static TextView note(View tile) {
        return tile.findViewById(R.id.note);
    }

    private static void set(View tile, String value, String note) {
        show(tile.findViewById(R.id.value), value);
        show(note(tile), note);
        note(tile).setVisibility(note.isEmpty() ? View.GONE : View.VISIBLE);
    }

    /** Text that changed (setting the same text again would lay the screen out again). */
    private static void show(TextView view, CharSequence text) {
        if (!text.toString().contentEquals(view.getText())) {
            view.setText(text);
        }
    }

    /** True if it changed. */
    private boolean pill(TextView view, String text, int fore, int back) {
        if (text.equals(view.getTag())) {
            return false;
        }
        view.setTag(text);
        view.setText(text);
        view.setTextColor(getColor(fore));
        GradientDrawable shape = (GradientDrawable) ContextCompat.getDrawable(this, R.drawable.bg_pill).mutate();
        shape.setColor(getColor(back));
        view.setBackground(shape);
        return true;
    }

    private Drawable tinted(int res, int colour, int sizeDp) {
        Drawable icon = ContextCompat.getDrawable(this, res).mutate();
        int size = Math.round(sizeDp * getResources().getDisplayMetrics().density);
        icon.setBounds(0, 0, size, size);
        icon.setTint(getColor(colour));
        return icon;
    }
}
