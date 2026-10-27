package com.cocfarm.app;

import android.os.Bundle;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import com.google.android.material.appbar.MaterialToolbar;

/** A screen of cards under a toolbar with a back arrow (see activity_page.xml). */
abstract class Page extends AppCompatActivity {
    protected MaterialToolbar toolbar;
    protected LinearLayout content;
    protected String folder;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setContentView(R.layout.activity_page);
        folder = getExternalFilesDir(null).getAbsolutePath();
        toolbar = findViewById(R.id.toolbar);
        toolbar.setNavigationOnClickListener(v -> finish());
        content = findViewById(R.id.content);
    }

    protected LinearLayout card(String heading) {
        return Ui.card(content, heading);
    }

    protected TextView text(String value, int appearance, int colour) {
        return Ui.text(this, value, appearance, colour);
    }

    protected View divider() {
        return Ui.divider(this);
    }

    protected int dp(float value) {
        return Ui.dp(this, value);
    }
}
