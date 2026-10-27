package com.cocfarm.app;

import android.content.Context;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.content.FileProvider;
import androidx.core.widget.NestedScrollView;

import com.google.android.material.button.MaterialButton;
import com.google.android.material.snackbar.Snackbar;

import org.json.JSONArray;

import java.io.File;

/**
 * The step-by-step log (the Windows app's "Details"): what the bot has been
 * doing, line by line, as it happens. Warnings in amber, errors in red,
 * attacks in green. The day's log file can be shared, for a bug report.
 */
final class LogPage {
    private static final int WARNING = 30, ERROR = 40;  // Python's logging levels
    private static final long POLL_MS = 1000;
    private final Context context;
    private final String folder;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SpannableStringBuilder shown = new SpannableStringBuilder();
    final NestedScrollView view;
    private final TextView lines;
    private int count;  // lines shown so far
    private boolean polling, asking;
    private final Runnable poll = new Runnable() {
        @Override
        public void run() {
            fetch();
            if (polling) main.postDelayed(this, POLL_MS);
        }
    };

    LogPage(Context context, String folder) {
        this.context = context;
        this.folder = folder;
        view = Ui.column(context);
        LinearLayout content = Ui.inside(view);
        LinearLayout top = new LinearLayout(context);
        top.setGravity(android.view.Gravity.CENTER_VERTICAL);
        top.setPadding(Ui.dp(context, 4), Ui.dp(context, 8), 0, 0);
        top.addView(Ui.text(context, "This run, as it happens.",
                        com.google.android.material.R.style.TextAppearance_Material3_BodySmall, R.color.cf_muted),
                new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        MaterialButton share = new MaterialButton(context, null, com.google.android.material.R.attr.borderlessButtonStyle);
        share.setText("Share the log file");
        share.setIconResource(R.drawable.ic_share);
        share.setOnClickListener(v -> share());
        top.addView(share);
        content.addView(top);
        lines = Ui.text(context, "Nothing yet. Start farming and each step appears here.",
                com.google.android.material.R.style.TextAppearance_Material3_BodySmall, R.color.cf_muted);
        lines.setTypeface(Typeface.MONOSPACE);
        lines.setTextIsSelectable(true);
        lines.setLineSpacing(0, 1.15f);
        lines.setPadding(Ui.dp(context, 14), Ui.dp(context, 12), Ui.dp(context, 14), Ui.dp(context, 12));
        Ui.card(content, "Log").addView(lines);
    }

    /** Follow the log while the page shows. */
    void start() {
        polling = true;
        main.removeCallbacks(poll);
        main.post(poll);
    }

    void stop() {
        polling = false;
        main.removeCallbacks(poll);
    }

    private void fetch() {
        if (asking) return;
        asking = true;
        new Thread(() -> {
            JSONArray answer = null;
            try {
                answer = new JSONArray(Bridge.python(context).getModule("cocfarm_android").callAttr("log_lines", count).toString());
            } catch (Exception ignored) {
                // Python still starting: next time
            }
            JSONArray got = answer;
            main.post(() -> {
                asking = false;
                if (got != null) add(got);
            });
        }, "log").start();
    }

    private void add(JSONArray answer) {
        JSONArray fresh = answer.optJSONArray(1);
        count = answer.optInt(0);
        if (fresh == null || fresh.length() == 0) return;
        boolean atEnd = !view.canScrollVertically(1);
        for (int i = 0; i < fresh.length(); i++) {
            JSONArray line = fresh.optJSONArray(i);
            int level = line.optInt(0);
            String text = line.optString(1);
            int colour = level >= ERROR ? R.color.cf_bad : level >= WARNING ? R.color.cf_warn
                    : text.contains("→ attack") || text.contains("done") ? R.color.cf_good : R.color.cf_text;
            int from = shown.length();
            shown.append(shown.length() == 0 ? "" : "\n").append(text);
            shown.setSpan(new ForegroundColorSpan(context.getColor(colour)), from, shown.length(),
                    Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        if (shown.length() > 200_000) {  // keep the screen light: the file has everything
            shown.delete(0, shown.length() - 150_000);
        }
        lines.setText(shown);
        if (atEnd) view.post(() -> view.fullScroll(NestedScrollView.FOCUS_DOWN));
    }

    private void share() {
        File log = new File(folder, "logs/farmbot.log");
        if (!log.exists()) {
            Snackbar.make(lines, "No log file yet: it starts with the first run.", Snackbar.LENGTH_SHORT).show();
            return;
        }
        Uri uri = FileProvider.getUriForFile(context, "com.cocfarm.app.files", log);
        context.startActivity(Intent.createChooser(new Intent(Intent.ACTION_SEND).setType("text/plain")
                .putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), "Share the log"));
    }
}
