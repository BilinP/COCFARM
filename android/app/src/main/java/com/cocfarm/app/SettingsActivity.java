package com.cocfarm.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.util.TypedValue;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.Switch;
import android.widget.TextView;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * Every setting, page by page. What's on it comes from cocfarm_settings.py,
 * and it's checked and saved there with the same rules as on Windows: a
 * change is saved a moment after it's made, and a mistake shows in red
 * under its setting. A farming bot picks the change up straight away.
 */
public class SettingsActivity extends Activity {
    private static final int GOOD = Color.parseColor("#6ccb5f"), BAD = Color.parseColor("#ff99a4"),
            MUTED = Color.parseColor("#9da5b4"), ACCENT = Color.parseColor("#4c9aff");
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Map<String, TextView> errors = new HashMap<>();
    private final List<View> dependents = new ArrayList<>();
    private final Map<View, JSONObject> dependsOn = new HashMap<>();
    private JSONArray pages;
    private JSONObject values;
    private LinearLayout tabs, content;
    private TextView saved;
    private int page;
    private String folder;
    private final Runnable save = this::save;
    private boolean pending;  // a change waiting to be saved

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        folder = getExternalFilesDir(null).getAbsolutePath();
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        try {
            JSONObject screen = new JSONObject(Python.getInstance().getModule("cocfarm_settings")
                    .callAttr("load", folder).toString());
            pages = screen.getJSONArray("pages");
            values = screen.getJSONObject("values");
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        root.setPadding(pad, pad, pad, 0);
        TextView title = text("Settings", 24, Color.WHITE);
        root.addView(title);
        saved = text("Changes save by themselves.", 13, MUTED);
        root.addView(saved);

        HorizontalScrollView strip = new HorizontalScrollView(this);
        tabs = new LinearLayout(this);
        strip.addView(tabs);
        root.addView(strip);
        ScrollView scroll = new ScrollView(this);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(0, 0, 0, dp(40));
        scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);
        showPage(0);
    }

    @Override
    protected void onPause() {
        if (pending) {  // leaving with a change not saved yet
            main.removeCallbacks(save);
            save();
        }
        super.onPause();
    }

    // ------------------------------------------------------------- pages

    private void showPage(int index) {
        page = index;
        tabs.removeAllViews();
        for (int i = 0; i < pages.length(); i++) {
            Button tab = new Button(this);
            tab.setText(pages.optJSONObject(i).optString("title"));
            tab.setAllCaps(false);
            tab.setTextColor(i == index ? ACCENT : Color.WHITE);
            int target = i;
            tab.setOnClickListener(v -> showPage(target));
            tabs.addView(tab);
        }
        content.removeAllViews();
        errors.clear();
        dependents.clear();
        dependsOn.clear();
        JSONArray groups = pages.optJSONObject(index).optJSONArray("groups");
        for (int g = 0; g < groups.length(); g++) {
            JSONObject group = groups.optJSONObject(g);
            TextView heading = text(group.optString("title"), 19, Color.WHITE);
            heading.setPadding(0, dp(22), 0, dp(4));
            content.addView(heading);
            JSONArray items = group.optJSONArray("items");
            for (int i = 0; i < items.length(); i++) {
                content.addView(item(items.optJSONObject(i)));
            }
        }
        refreshDependents();
    }

    private View item(JSONObject item) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(0, dp(10), 0, dp(6));
        String kind = item.optString("kind"), path = item.optString("path");
        TextView title = text(item.optString("title"), 16, Color.WHITE);
        View control = control(kind, path, item);
        if (kind.equals("switch")) {  // title and switch on one line
            LinearLayout row = new LinearLayout(this);
            row.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
            row.addView(control);
            box.addView(row);
        } else {
            box.addView(title);
        }
        String help = item.optString("help");
        if (!help.isEmpty()) {
            box.addView(text(help, 13, MUTED));
        }
        if (!kind.equals("switch")) {
            box.addView(control);
        }
        TextView error = text("", 13, BAD);
        error.setVisibility(View.GONE);
        box.addView(error);
        if (kind.equals("range")) {
            errors.put(item.optString("low"), error);
            errors.put(item.optString("high"), error);
        } else {
            errors.put(path, error);
        }
        JSONObject depends = item.optJSONObject("depends");
        if (depends != null) {
            dependents.add(box);
            dependsOn.put(box, depends);
        }
        return box;
    }

    private View control(String kind, String path, JSONObject item) {
        switch (kind) {
            case "switch": {
                Switch toggle = new Switch(this);
                toggle.setChecked(values.optBoolean(path));
                toggle.setOnCheckedChangeListener((v, on) -> changed(path, on));
                return toggle;
            }
            case "speed": {
                LinearLayout row = new LinearLayout(this);
                TextView slow = text("Slow", 13, MUTED), fast = text("Fast", 13, MUTED);
                SeekBar bar = new SeekBar(this);
                bar.setMax(8);
                bar.setProgress(values.optInt(path, 5) - 1);
                bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                    public void onProgressChanged(SeekBar s, int p, boolean user) {
                        if (user) changed(path, String.valueOf(p + 1));
                    }

                    public void onStartTrackingTouch(SeekBar s) {
                    }

                    public void onStopTrackingTouch(SeekBar s) {
                    }
                });
                row.addView(slow);
                row.addView(bar, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
                row.addView(fast);
                return row;
            }
            case "choice":
                return choice(path, options(item));
            case "multi": {
                List<String[]> options = options(item);
                if (options.size() <= 4) {
                    return checkboxes(path, options);
                }
                return listButton(path, options, false);
            }
            case "ordered":
                return listButton(path, options(item), true);
            case "hours":
                return hoursButton(path);
            case "range": {
                LinearLayout row = new LinearLayout(this);
                row.addView(box(item.optString("low"), "number"));
                row.addView(text("  to  ", 15, Color.WHITE));
                row.addView(box(item.optString("high"), "number"));
                row.addView(text("  " + item.optString("suffix"), 15, MUTED));
                return row;
            }
            default: {  // amount, number, text
                if (!item.optString("suffix").isEmpty()) {
                    LinearLayout row = new LinearLayout(this);
                    row.addView(box(path, kind));
                    row.addView(text("  " + item.optString("suffix"), 15, MUTED));
                    return row;
                }
                return box(path, kind);
            }
        }
    }

    private EditText box(String path, String kind) {
        EditText edit = new EditText(this);
        edit.setText(values.optString(path));
        edit.setSingleLine(true);
        if (kind.equals("number")) {
            edit.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
            edit.setMinWidth(dp(90));
        } else if (kind.equals("amount")) {
            edit.setMinWidth(dp(160));
            edit.setHint("e.g. 500k or 1.5m");
        } else {
            edit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        }
        edit.addTextChangedListener(new TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void onTextChanged(CharSequence s, int a, int b, int c) {
            }

            public void afterTextChanged(Editable s) {
                changed(path, s.toString());
            }
        });
        return edit;
    }

    private View choice(String path, List<String[]> options) {
        List<String> keys = new ArrayList<>(), labels = new ArrayList<>();
        for (String[] option : options) {
            keys.add(option[0]);
            labels.add(option[1]);
        }
        Spinner spinner = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, labels);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter);
        int current = keys.indexOf(values.optString(path));
        spinner.setSelection(Math.max(current, 0), false);
        spinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            public void onItemSelected(AdapterView<?> p, View v, int position, long id) {
                if (!keys.get(position).equals(values.optString(path))) {
                    changed(path, keys.get(position));
                }
            }

            public void onNothingSelected(AdapterView<?> p) {
            }
        });
        return spinner;
    }

    private View checkboxes(String path, List<String[]> options) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        List<String> chosen = list(path);
        for (String[] option : options) {
            String key = option[0];
            CheckBox box = new CheckBox(this);
            box.setText(option[1]);
            box.setChecked(chosen.contains(key));
            box.setOnCheckedChangeListener((v, on) -> {
                List<String> now = list(path);
                now.remove(key);
                if (on) now.add(key);
                changed(path, new JSONArray(now));
            });
            row.addView(box);
        }
        return row;
    }

    /** A list too long for checkboxes: a button showing the choice, opening a picker. */
    private View listButton(String path, List<String[]> options, boolean ordered) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(summary(list(path), options));
        button.setOnClickListener(v -> {
            List<String> keys = new ArrayList<>(), now = list(path);
            String[] labels = new String[options.size()];
            boolean[] checked = new boolean[options.size()];
            for (int i = 0; i < options.size(); i++) {
                keys.add(options.get(i)[0]);
                labels[i] = options.get(i)[1];
                checked[i] = now.contains(keys.get(i));
            }
            new AlertDialog.Builder(this)
                    .setTitle(ordered ? "Tap them in the order to give them" : "Pick")
                    .setMultiChoiceItems(labels, checked, (d, which, on) -> {
                        now.remove(keys.get(which));  // a new pick goes last: the order they're tapped in
                        if (on) now.add(keys.get(which));
                    })
                    .setPositiveButton("Done", (d, w) -> {
                        changed(path, new JSONArray(now));
                        button.setText(summary(now, options));
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
        });
        return button;
    }

    private View hoursButton(String path) {
        List<String[]> options = new ArrayList<>();
        for (int h = 0; h < 24; h++) {
            options.add(new String[]{String.valueOf(h), String.format("%02d:00 to %02d:59", h, h)});
        }
        return listButton(path, options, false);
    }

    private String summary(List<String> chosen, List<String[]> options) {
        if (chosen.isEmpty()) {
            return "None picked: tap to pick";
        }
        if (options.size() == 24) {  // hours: as spans of the day
            return hours(chosen);
        }
        List<String> labels = new ArrayList<>();
        for (String key : chosen) {
            String label = key;
            for (String[] option : options) {
                if (option[0].equals(key)) label = option[1];
            }
            labels.add(label.replace(" to ", "–").replace(":59", ""));
        }
        return TextUtils.join(", ", labels);
    }

    // ------------------------------------------------------------- saving

    private void changed(String path, Object value) {
        try {
            values.put(path, value);
        } catch (JSONException e) {
            return;
        }
        refreshDependents();
        saved.setText("Saving…");
        saved.setTextColor(MUTED);
        pending = true;
        main.removeCallbacks(save);
        main.postDelayed(save, 700);
    }

    private void save() {
        pending = false;
        String form = values.toString();
        new Thread(() -> {
            String result;
            try {
                result = Python.getInstance().getModule("cocfarm_settings").callAttr("save", folder, form).toString();
            } catch (Throwable e) {  // e.g. the file can't be written: say so rather than close the app
                result = "Couldn't save: " + e.getMessage();
            }
            String shown = result;
            main.post(() -> showErrors(shown));
        }, "save-settings").start();
    }

    private void showErrors(String result) {
        try {
            JSONObject problems = new JSONObject(result);
            for (Map.Entry<String, TextView> entry : errors.entrySet()) {
                entry.getValue().setVisibility(View.GONE);
            }
            int shown = 0;
            for (Iterator<String> it = problems.keys(); it.hasNext(); ) {
                String path = it.next();
                TextView error = errors.get(path);
                if (error != null) {
                    error.setText("✗ " + problems.optString(path));
                    error.setVisibility(View.VISIBLE);
                    shown++;
                }
            }
            if (problems.length() == 0) {
                saved.setText("✓ Saved");
                saved.setTextColor(GOOD);
            } else {
                saved.setText("✗ Not saved: fix the setting in red" + (shown < problems.length() ? " (on another page)" : ""));
                saved.setTextColor(BAD);
            }
        } catch (JSONException e) {
            saved.setText("✗ " + result);
            saved.setTextColor(BAD);
        }
    }

    private void refreshDependents() {
        for (View view : dependents) {
            JSONObject depends = dependsOn.get(view);
            Object want = depends.opt("is"), have = values.opt(depends.optString("path"));
            boolean on = String.valueOf(want).equals(String.valueOf(have));
            view.setAlpha(on ? 1f : 0.4f);
            enable(view, on);
        }
    }

    private void enable(View view, boolean on) {
        view.setEnabled(on);
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) {
                enable(group.getChildAt(i), on);
            }
        }
    }

    // ------------------------------------------------------------- helpers

    private List<String> list(String path) {
        List<String> out = new ArrayList<>();
        JSONArray array = values.optJSONArray(path);
        for (int i = 0; array != null && i < array.length(); i++) {
            out.add(array.optString(i));
        }
        return out;
    }

    /** Hours as spans: "All day", or "08:00–16:59, 20:00–23:59". */
    private static String hours(List<String> chosen) {
        boolean[] on = new boolean[24];
        for (String hour : chosen) {
            on[Integer.parseInt(hour)] = true;
        }
        List<String> spans = new ArrayList<>();
        for (int h = 0; h < 24; h++) {
            if (!on[h]) continue;
            int end = h;
            while (end + 1 < 24 && on[end + 1]) end++;
            spans.add(String.format("%02d:00–%02d:59", h, end));
            h = end;
        }
        return spans.size() == 1 && chosen.size() == 24 ? "All day" : TextUtils.join(", ", spans);
    }

    /** An item's options, in order: [value, label] pairs. */
    private static List<String[]> options(JSONObject item) {
        List<String[]> out = new ArrayList<>();
        JSONArray pairs = item.optJSONArray("options");
        for (int i = 0; pairs != null && i < pairs.length(); i++) {
            JSONArray pair = pairs.optJSONArray(i);
            out.add(new String[]{pair.optString(0), pair.optString(1)});
        }
        return out;
    }

    private TextView text(String value, int sp, int colour) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        view.setTextColor(colour);
        return view;
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }
}
