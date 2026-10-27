package com.cocfarm.app;

import android.content.Context;
import android.content.ContextWrapper;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.GridLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.widget.NestedScrollView;

import com.google.android.material.button.MaterialButton;
import com.google.android.material.card.MaterialCardView;
import com.google.android.material.chip.Chip;
import com.google.android.material.chip.ChipGroup;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.divider.MaterialDivider;
import com.google.android.material.materialswitch.MaterialSwitch;
import com.google.android.material.slider.Slider;
import com.google.android.material.textfield.MaterialAutoCompleteTextView;
import com.google.android.material.textfield.TextInputLayout;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * Every setting, page by page: each page is one of the home screen's tabs.
 * What's on it comes from cocfarm_settings.py, and it's checked and saved
 * there with the same rules as on Windows: a change is saved a moment after
 * it's made, and a mistake shows in red on its setting. A farming bot picks
 * the change up straight away. (Built in the home screen's context.)
 */
final class SettingsForm extends ContextWrapper {
    private final Handler main = new Handler(Looper.getMainLooper());
    // Setting path -> shows (or, given null, clears) its mistake.
    private final Map<String, Consumer<String>> errors = new HashMap<>();
    private final List<View> dependents = new ArrayList<>();
    private final Map<View, JSONObject> dependsOn = new HashMap<>();
    private JSONArray pages;
    private JSONObject values;
    private LinearLayout content;
    private final TextView statusLine;
    private final String folder;
    private final Runnable save = this::save;
    private boolean pending;  // a change waiting to be saved

    SettingsForm(Context screen, String folder, TextView statusLine) {
        super(screen);
        this.folder = folder;
        this.statusLine = statusLine;
    }

    /** Read the settings (Python: off the main thread). */
    void load() throws JSONException {
        JSONObject screen = new JSONObject(Bridge.python(this).getModule("cocfarm_settings")
                .callAttr("load", folder).toString());
        pages = screen.getJSONArray("pages");
        values = screen.getJSONObject("values");
    }

    int pageCount() {
        return pages.length();
    }

    String title(int page) {
        return pages.optJSONObject(page).optString("title");
    }

    /** Save a change still waiting (the screen is going away). */
    void flush() {
        if (pending) {
            main.removeCallbacks(save);
            save();
        }
    }

    // ------------------------------------------------------------- pages

    /** A page of settings, built afresh (the controls of the page shown before are let go). */
    View page(int index) {
        NestedScrollView scroll = Ui.column(this);
        content = Ui.inside(scroll);
        errors.clear();
        dependents.clear();
        dependsOn.clear();
        TextView note = text("Changes save by themselves.", com.google.android.material.R.style.TextAppearance_Material3_BodySmall,
                R.color.cf_muted);
        note.setPadding(dp(4), dp(16), dp(4), 0);
        content.addView(note);
        JSONArray groups = pages.optJSONObject(index).optJSONArray("groups");
        for (int g = 0; g < groups.length(); g++) {
            JSONObject group = groups.optJSONObject(g);
            TextView heading = new TextView(this);
            heading.setTextAppearance(R.style.TextAppearance_COCFARM_Overline);
            heading.setText(group.optString("title"));
            heading.setPadding(dp(4), dp(20), 0, dp(8));
            content.addView(heading);
            MaterialCardView card = new MaterialCardView(this);
            LinearLayout items = new LinearLayout(this);
            items.setOrientation(LinearLayout.VERTICAL);
            items.setPadding(dp(16), dp(4), dp(16), dp(4));
            card.addView(items);
            content.addView(card);
            JSONArray list = group.optJSONArray("items");
            for (int i = 0; i < list.length(); i++) {
                if (i > 0) {
                    MaterialDivider divider = new MaterialDivider(this);
                    divider.setDividerColor(getColor(R.color.cf_outline_variant));
                    items.addView(divider);
                }
                items.addView(item(list.optJSONObject(i)));
            }
        }
        refreshDependents();
        return scroll;
    }

    private View item(JSONObject item) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(0, dp(14), 0, dp(14));
        String kind = item.optString("kind"), path = item.optString("path");
        LinearLayout words = new LinearLayout(this);
        words.setOrientation(LinearLayout.VERTICAL);
        words.addView(text(item.optString("title"), com.google.android.material.R.style.TextAppearance_Material3_TitleSmall, R.color.cf_text));
        String help = item.optString("help");
        if (!help.isEmpty()) {
            TextView helpText = text(help, com.google.android.material.R.style.TextAppearance_Material3_BodySmall, R.color.cf_muted);
            helpText.setPadding(0, dp(2), 0, 0);
            words.addView(helpText);
        }
        TextView error = text("", com.google.android.material.R.style.TextAppearance_Material3_BodySmall, R.color.cf_bad);
        error.setPadding(0, dp(6), 0, 0);
        error.setVisibility(View.GONE);
        Consumer<String> showError = message -> {
            error.setText(message == null ? "" : message);
            error.setVisibility(message == null ? View.GONE : View.VISIBLE);
        };
        if (kind.equals("button")) {  // does something: its title is on the button, the answer under it
            words.removeViewAt(0);
            box.addView(words);
            box.addView(actionButton(item, error));
            box.addView(error);
            return box;
        }
        if (kind.equals("switch")) {  // the words and the switch on one line; the whole line flips it
            MaterialSwitch toggle = new MaterialSwitch(this);
            toggle.setChecked(values.optBoolean(path));
            toggle.setOnCheckedChangeListener((v, on) -> changed(path, on));
            LinearLayout row = new LinearLayout(this);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.addView(words, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
            row.addView(toggle);
            row.setOnClickListener(v -> toggle.toggle());
            box.addView(row);
            errors.put(path, showError);
        } else {
            box.addView(words);
            box.addView(control(kind, path, item, showError));
        }
        box.addView(error);
        JSONObject depends = item.optJSONObject("depends");
        if (depends != null) {
            dependents.add(box);
            dependsOn.put(box, depends);
        }
        return box;
    }

    private View control(String kind, String path, JSONObject item, Consumer<String> showError) {
        switch (kind) {
            case "speed":
                errors.put(path, showError);
                return speed(path);
            case "choice":
                errors.put(path, showError);
                return choice(path, options(item));
            case "multi": {
                errors.put(path, showError);
                List<String[]> options = options(item);
                return options.size() <= 6 ? chips(path, options) : picker(path, options, false);
            }
            case "ordered":
                errors.put(path, showError);
                return picker(path, options(item), true);
            case "hours":
                errors.put(path, showError);
                return hoursPicker(path);
            case "range": {
                LinearLayout row = new LinearLayout(this);
                row.setGravity(Gravity.CENTER_VERTICAL);
                TextInputLayout low = field(item.optString("low"), "number", "From", "");
                TextInputLayout high = field(item.optString("high"), "number", "To", item.optString("suffix"));
                row.addView(low, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
                View gap = new View(this);
                row.addView(gap, new LinearLayout.LayoutParams(dp(12), 1));
                row.addView(high, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
                return row;
            }
            default:  // amount, number, text
                return field(path, kind, "", item.optString("suffix"));
        }
    }

    /** A button that runs one of cocfarm_settings' actions (test_alert, check_update); its answer shows in ``answer``. */
    private View actionButton(JSONObject item, TextView answer) {
        MaterialButton button = new MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle);
        button.setText(item.optString("title"));
        LinearLayout.LayoutParams spot = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        spot.topMargin = dp(6);
        button.setLayoutParams(spot);
        String action = item.optString("action");
        button.setOnClickListener(v -> {
            button.setEnabled(false);
            answer.setTextColor(getColor(R.color.cf_muted));
            answer.setText("Working…");
            answer.setVisibility(View.VISIBLE);
            main.removeCallbacks(save);
            String form = values.toString();
            new Thread(() -> {
                String said;
                int colour = R.color.cf_text;
                try {
                    // A change still waiting (say, a webhook just pasted) is saved first.
                    com.chaquo.python.PyObject settings = Bridge.python(this).getModule("cocfarm_settings");
                    settings.callAttr("save", folder, form);
                    pending = false;
                    if (action.equals("check_update")) {
                        JSONObject found = new JSONObject(settings.callAttr("check_update", folder, true).toString());
                        said = found.isNull("version") ? found.optString("note")
                                : "COCFARM " + found.optString("version") + " is out: update it from the home screen.";
                        colour = found.isNull("version") ? R.color.cf_text : R.color.cf_good;
                    } else {
                        said = settings.callAttr(action, folder).toString();
                        colour = said.startsWith("✓") ? R.color.cf_good : said.startsWith("✗") ? R.color.cf_bad : R.color.cf_text;
                    }
                } catch (Exception e) {
                    said = "✗ " + e.getMessage();
                    colour = R.color.cf_bad;
                }
                String text = said;
                int tone = colour;
                main.post(() -> {
                    button.setEnabled(true);
                    answer.setText(text);
                    answer.setTextColor(getColor(tone));
                });
            }, action).start();
        });
        return button;
    }

    private TextInputLayout field(String path, String kind, String label, String suffix) {
        TextInputLayout layout = (TextInputLayout) LayoutInflater.from(this).inflate(R.layout.field_text, null, false);
        TextView edit = layout.getEditText();
        edit.setText(values.optString(path));
        if (!label.isEmpty()) {
            layout.setHint(label);
        }
        if (!suffix.isEmpty()) {
            layout.setSuffixText(suffix);
        }
        if (kind.equals("number")) {
            edit.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
        } else if (kind.equals("amount")) {
            edit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
            layout.setPlaceholderText("e.g. 500k or 1.5m");
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
        errors.put(path, layout::setError);
        return layout;
    }

    private View speed(String path) {
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        Slider slider = new Slider(this);
        slider.setValueFrom(1);
        slider.setValueTo(9);
        slider.setStepSize(1);
        slider.setValue(Math.max(1, Math.min(9, values.optInt(path, 5))));
        slider.setLabelFormatter(value -> String.valueOf(Math.round(value)));
        slider.addOnChangeListener((s, value, user) -> {
            if (user) changed(path, String.valueOf(Math.round(value)));
        });
        column.addView(slider);
        LinearLayout ends = new LinearLayout(this);
        ends.setPadding(dp(14), 0, dp(14), 0);
        ends.addView(text("Slow and careful", com.google.android.material.R.style.TextAppearance_Material3_LabelSmall, R.color.cf_muted),
                new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        ends.addView(text("Fast", com.google.android.material.R.style.TextAppearance_Material3_LabelSmall, R.color.cf_muted));
        column.addView(ends);
        return column;
    }

    /** A few options as chips (one picked); more, in a drop-down. */
    private View choice(String path, List<String[]> options) {
        List<String> keys = new ArrayList<>(), labels = new ArrayList<>();
        for (String[] option : options) {
            keys.add(option[0]);
            labels.add(option[1]);
        }
        if (options.size() <= 4) {
            ChipGroup group = new ChipGroup(this);
            group.setPadding(0, dp(8), 0, 0);
            group.setSingleSelection(true);
            group.setSelectionRequired(true);
            for (int i = 0; i < options.size(); i++) {
                Chip chip = (Chip) LayoutInflater.from(this).inflate(R.layout.chip_filter, group, false);
                chip.setId(View.generateViewId());
                chip.setText(labels.get(i));
                group.addView(chip);
                String key = keys.get(i);
                if (key.equals(values.optString(path))) {
                    group.check(chip.getId());
                }
                chip.setOnCheckedChangeListener((v, on) -> {
                    if (on && !key.equals(values.optString(path))) changed(path, key);
                });
            }
            return group;
        }
        TextInputLayout layout = (TextInputLayout) LayoutInflater.from(this).inflate(R.layout.field_dropdown, null, false);
        MaterialAutoCompleteTextView menu = (MaterialAutoCompleteTextView) layout.getEditText();
        menu.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, labels));
        int current = keys.indexOf(values.optString(path));
        menu.setText(labels.get(Math.max(current, 0)), false);
        menu.setOnItemClickListener((p, v, position, id) -> {
            if (!keys.get(position).equals(values.optString(path))) changed(path, keys.get(position));
        });
        return layout;
    }

    private View chips(String path, List<String[]> options) {
        ChipGroup group = new ChipGroup(this);
        group.setPadding(0, dp(8), 0, 0);
        List<String> chosen = list(path);
        for (String[] option : options) {
            String key = option[0];
            Chip chip = (Chip) LayoutInflater.from(this).inflate(R.layout.chip_filter, group, false);
            chip.setText(option[1]);
            chip.setChecked(chosen.contains(key));
            chip.setOnCheckedChangeListener((v, on) -> {
                List<String> now = list(path);
                now.remove(key);
                if (on) now.add(key);
                changed(path, new JSONArray(now));
            });
            group.addView(chip);
        }
        return group;
    }

    /** A list too long to show: what's picked, opening a dialog to change it. */
    private View picker(String path, List<String[]> options, boolean ordered) {
        View row = LayoutInflater.from(this).inflate(R.layout.row_picker, null, false);
        TextView summary = row.findViewById(R.id.summary);
        summary.setText(summary(list(path), options, ordered));
        row.setOnClickListener(v -> {
            List<String> keys = new ArrayList<>(), now = list(path);
            String[] labels = new String[options.size()];
            boolean[] checked = new boolean[options.size()];
            for (int i = 0; i < options.size(); i++) {
                keys.add(options.get(i)[0]);
                labels[i] = options.get(i)[1];
                checked[i] = now.contains(keys.get(i));
            }
            new MaterialAlertDialogBuilder(this)
                    .setTitle(ordered ? "Tap them in the order to give them" : "Pick")
                    .setMultiChoiceItems(labels, checked, (d, which, on) -> {
                        now.remove(keys.get(which));  // a new pick goes last: the order they're tapped in
                        if (on) now.add(keys.get(which));
                    })
                    .setPositiveButton("Done", (d, w) -> {
                        changed(path, new JSONArray(now));
                        summary.setText(summary(now, options, ordered));
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
        });
        return row;
    }

    /** The hours to play in: a grid of the day's 24 hours. */
    private View hoursPicker(String path) {
        View row = LayoutInflater.from(this).inflate(R.layout.row_picker, null, false);
        TextView summary = row.findViewById(R.id.summary);
        summary.setText(hours(list(path)));
        row.setOnClickListener(v -> {
            List<String> now = list(path);
            GridLayout grid = new GridLayout(this);
            grid.setColumnCount(4);
            grid.setPadding(dp(20), dp(8), dp(20), 0);
            List<Chip> chips = new ArrayList<>();
            for (int h = 0; h < 24; h++) {
                Chip chip = (Chip) LayoutInflater.from(this).inflate(R.layout.chip_filter, grid, false);
                chip.setText(String.format("%02d:00", h));
                chip.setCheckedIconVisible(false);
                chip.setTextAlignment(View.TEXT_ALIGNMENT_CENTER);
                chip.setChipStartPadding(0);
                chip.setChipEndPadding(0);
                chip.setTextStartPadding(dp(2));
                chip.setTextEndPadding(dp(2));
                chip.setChecked(now.contains(String.valueOf(h)));
                chips.add(chip);
                GridLayout.LayoutParams cell = new GridLayout.LayoutParams(GridLayout.spec(GridLayout.UNDEFINED),
                        GridLayout.spec(GridLayout.UNDEFINED, 1f));
                cell.width = 0;
                cell.setMargins(dp(3), 0, dp(3), 0);
                grid.addView(chip, cell);
            }
            new MaterialAlertDialogBuilder(this)
                    .setTitle("Hours to play")
                    .setMessage("Tap the hours it may play in (this device's time).")
                    .setView(grid)
                    .setPositiveButton("Done", (d, w) -> {
                        List<String> picked = new ArrayList<>();
                        for (int h = 0; h < 24; h++) {
                            if (chips.get(h).isChecked()) picked.add(String.valueOf(h));
                        }
                        changed(path, new JSONArray(picked));
                        summary.setText(hours(picked));
                    })
                    .setNeutralButton("All day", (d, w) -> {
                        List<String> all = new ArrayList<>();
                        for (int h = 0; h < 24; h++) all.add(String.valueOf(h));
                        changed(path, new JSONArray(all));
                        summary.setText(hours(all));
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
        });
        return row;
    }

    private String summary(List<String> chosen, List<String[]> options, boolean ordered) {
        if (chosen.isEmpty()) {
            return "None: tap to pick";
        }
        List<String> labels = new ArrayList<>();
        for (String key : chosen) {
            String label = key;
            for (String[] option : options) {
                if (option[0].equals(key)) label = option[1];
            }
            labels.add(ordered ? (labels.size() + 1) + ". " + label : label);
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
        status("Saving…", R.color.cf_muted);
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
                result = Bridge.python(this).getModule("cocfarm_settings").callAttr("save", folder, form).toString();
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
            for (Consumer<String> error : errors.values()) {
                error.accept(null);
            }
            int shown = 0;
            for (Iterator<String> it = problems.keys(); it.hasNext(); ) {
                String path = it.next();
                Consumer<String> error = errors.get(path);
                if (error != null) {
                    error.accept(problems.optString(path));
                    shown++;
                }
            }
            if (problems.length() == 0) {
                status("✓ Saved", R.color.cf_good);
            } else {
                status("Not saved: fix the setting in red" + (shown < problems.length() ? " (on another tab)" : ""),
                        R.color.cf_bad);
            }
        } catch (JSONException e) {
            status(result, R.color.cf_bad);
        }
    }

    private void status(String text, int colour) {
        statusLine.setText(text);
        statusLine.setTextColor(getColor(colour));
        statusLine.setVisibility(View.VISIBLE);
    }

    private void refreshDependents() {
        for (View view : dependents) {
            JSONObject depends = dependsOn.get(view);
            Object want = depends.opt("is"), have = values.opt(depends.optString("path"));
            boolean on;
            if (want instanceof JSONArray) {
                on = false;
                for (int i = 0; i < ((JSONArray) want).length(); i++) {
                    on |= String.valueOf(((JSONArray) want).opt(i)).equals(String.valueOf(have));
                }
            } else {
                on = String.valueOf(want).equals(String.valueOf(have));
            }
            view.animate().alpha(on ? 1f : 0.38f).setDuration(150).start();
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
        if (chosen.isEmpty()) {
            return "None: tap to pick";
        }
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

    private TextView text(String value, int appearance, int colour) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextAppearance(appearance);
        view.setTextColor(getColor(colour));
        return view;
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }
}
