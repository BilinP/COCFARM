package com.cocfarm.app;

import java.util.Locale;

/** Numbers and times as the screens show them (as the Windows app does). */
final class Fmt {
    private Fmt() {
    }

    /** 1234567 -> "1.23M", 45600 -> "45.6k", 950 -> "950". */
    static String shortNumber(long value) {
        long size = Math.abs(value);
        String sign = value < 0 ? "-" : "";
        if (size >= 1_000_000) {
            return sign + trim(String.format(Locale.US, "%.2f", size / 1e6)) + "M";
        }
        if (size >= 10_000) {
            return sign + trim(String.format(Locale.US, "%.1f", size / 1e3)) + "k";
        }
        return String.format(Locale.US, "%,d", value);
    }

    /** Like shortNumber, with a + for a gain. */
    static String gain(long value) {
        return (value > 0 ? "+" : "") + shortNumber(value);
    }

    static String whole(long value) {
        return String.format(Locale.US, "%,d", value);
    }

    /** 3725 -> "1h 02m", 125 -> "2m 05s". */
    static String duration(long seconds) {
        if (seconds >= 3600) {
            return String.format(Locale.US, "%dh %02dm", seconds / 3600, seconds % 3600 / 60);
        }
        return String.format(Locale.US, "%dm %02ds", seconds / 60, seconds % 60);
    }

    private static String trim(String number) {
        return number.contains(".") ? number.replaceAll("0+$", "").replaceAll("\\.$", "") : number;
    }
}
