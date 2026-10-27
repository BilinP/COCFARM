package com.cocfarm.app;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * What the bot made of the last base it scouted (the Windows app's "Look at
 * a base"): the loot it read, the troop cards, and where troops can go,
 * drawn over the base.
 */
public class SeenActivity extends Page {
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        toolbar.setTitle("What the bot saw");
        toolbar.setSubtitle("Looking at the last base…");
        Handler main = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            JSONObject seen;
            Bitmap picture = null;
            try {
                seen = new JSONObject(Bridge.python(this).getModule("cocfarm_settings")
                        .callAttr("look_at_base", folder).toString());
                if (seen.has("picture")) {
                    picture = BitmapFactory.decodeFile(seen.optString("picture"));
                }
            } catch (Exception e) {
                main.post(() -> toolbar.setSubtitle("Couldn't look at it: " + e.getMessage()));
                return;
            }
            JSONObject shown = seen;
            Bitmap bitmap = picture;
            main.post(() -> show(shown, bitmap));
        }, "look").start();
    }

    private void show(JSONObject seen, Bitmap picture) {
        toolbar.setSubtitle("The last base scouted");
        if (picture != null) {
            LinearLayout card = card("Base");
            ImageView image = new ImageView(this);
            image.setAdjustViewBounds(true);
            image.setImageBitmap(picture);
            image.setContentDescription("The base, with the red line, deploy points and troop cards drawn on");
            card.addView(image);
        }
        LinearLayout found = card("Found");
        JSONArray lines = seen.optJSONArray("lines");
        for (int i = 0; lines != null && i < lines.length(); i++) {
            if (i > 0) found.addView(divider());
            TextView line = text(lines.optString(i), com.google.android.material.R.style.TextAppearance_Material3_BodyMedium,
                    R.color.cf_text);
            line.setPadding(dp(16), dp(12), dp(16), dp(12));
            found.addView(line);
        }
        TextView note = text("Green: where troops are dropped. Boxes: the troop cards it found, by kind.",
                com.google.android.material.R.style.TextAppearance_Material3_BodySmall, R.color.cf_muted);
        note.setPadding(dp(4), dp(12), dp(4), 0);
        content.addView(note);
    }
}
