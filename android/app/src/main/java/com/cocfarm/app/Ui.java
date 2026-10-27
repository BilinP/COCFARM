package com.cocfarm.app;

import android.content.Context;
import android.util.TypedValue;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.widget.NestedScrollView;

import com.google.android.material.card.MaterialCardView;
import com.google.android.material.divider.MaterialDivider;

/** The pieces the screens are built from: a scrolling column, headed cards, text, dividers. */
final class Ui {
    private Ui() {
    }

    /** A scrolling column; the column is the scroll view's only child. */
    static NestedScrollView column(Context context) {
        NestedScrollView scroll = new NestedScrollView(context);
        LinearLayout column = new LinearLayout(context);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(dp(context, 16), 0, dp(context, 16), dp(context, 32));
        scroll.addView(column);
        return scroll;
    }

    static LinearLayout inside(NestedScrollView scroll) {
        return (LinearLayout) scroll.getChildAt(0);
    }

    /** A heading, and a card under it to fill. */
    static LinearLayout card(LinearLayout column, String heading) {
        Context context = column.getContext();
        TextView title = new TextView(context);
        title.setTextAppearance(R.style.TextAppearance_COCFARM_Overline);
        title.setText(heading);
        title.setPadding(dp(context, 4), dp(context, 20), 0, dp(context, 8));
        column.addView(title);
        MaterialCardView card = new MaterialCardView(context);
        LinearLayout inside = new LinearLayout(context);
        inside.setOrientation(LinearLayout.VERTICAL);
        card.addView(inside);
        column.addView(card);
        return inside;
    }

    static TextView text(Context context, String value, int appearance, int colour) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextAppearance(appearance);
        view.setTextColor(context.getColor(colour));
        return view;
    }

    static View divider(Context context) {
        MaterialDivider divider = new MaterialDivider(context);
        divider.setDividerColor(context.getColor(R.color.cf_outline_variant));
        LinearLayout.LayoutParams spot = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        spot.setMarginStart(dp(context, 16));
        spot.setMarginEnd(dp(context, 16));
        divider.setLayoutParams(spot);
        return divider;
    }

    static int dp(Context context, float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                context.getResources().getDisplayMetrics()));
    }
}
