package com.cocfarm.app;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.util.TypedValue;
import android.view.View;

/** Bars side by side, each with a label under it (the History screen's days). */
final class BarChart extends View {
    private final Paint bar = new Paint(Paint.ANTI_ALIAS_FLAG), empty = new Paint(Paint.ANTI_ALIAS_FLAG),
            label = new Paint(Paint.ANTI_ALIAS_FLAG), top = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF box = new RectF();
    private long[] values = new long[0];
    private String[] labels = new String[0];
    private int highlight = -1;  // today's bar

    BarChart(Context context) {
        super(context);
        bar.setColor(context.getColor(R.color.cf_accent));
        empty.setColor(context.getColor(R.color.cf_surface_highest));
        label.setColor(context.getColor(R.color.cf_muted));
        label.setTextSize(sp(11));
        label.setTextAlign(Paint.Align.CENTER);
        top.setColor(context.getColor(R.color.cf_text));
        top.setTextSize(sp(10));
        top.setTextAlign(Paint.Align.CENTER);
    }

    void show(long[] values, String[] labels, int highlight) {
        this.values = values;
        this.labels = labels;
        this.highlight = highlight;
        invalidate();
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        setMeasuredDimension(MeasureSpec.getSize(widthSpec), Math.round(sp(170)));
    }

    @Override
    protected void onDraw(Canvas canvas) {
        int n = values.length;
        if (n == 0) {
            return;
        }
        long most = 1;
        for (long v : values) most = Math.max(most, v);
        float pad = sp(12), labelRoom = sp(22), topRoom = sp(16);
        float slot = (getWidth() - 2 * pad) / n, width = slot * 0.62f;
        float bottom = getHeight() - labelRoom, height = bottom - topRoom;
        for (int i = 0; i < n; i++) {
            float cx = pad + slot * (i + 0.5f);
            float h = values[i] == 0 ? sp(3) : Math.max(sp(3), height * values[i] / most);
            box.set(cx - width / 2, bottom - h, cx + width / 2, bottom);
            Paint fill = values[i] == 0 ? empty : bar;
            fill.setAlpha(values[i] == 0 || i == highlight ? 255 : 170);
            canvas.drawRoundRect(box, sp(4), sp(4), fill);
            canvas.drawText(labels[i], cx, getHeight() - sp(6), label);
            if (values[i] > 0 && (i == highlight || values[i] == most)) {
                canvas.drawText(Fmt.shortNumber(values[i]), cx, bottom - h - sp(4), top);
            }
        }
    }

    private float sp(float value) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value, getResources().getDisplayMetrics());
    }
}
