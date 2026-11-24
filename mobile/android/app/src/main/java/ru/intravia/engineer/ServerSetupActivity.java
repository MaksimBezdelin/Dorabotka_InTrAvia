package ru.intravia.engineer;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Стартовая нативная плашка с адресом сервера — по образцу референсного APK.
 * Адрес запоминается; при следующих запусках плашка пропускается.
 */
public class ServerSetupActivity extends AppCompatActivity {

    public static final String PREFS_NAME = "intravia_prefs";
    public static final String PREF_SERVER_URL = "server_url";
    public static final String PREF_SETUP_VERSION = "setup_version";
    public static final String EXTRA_FORCE_SETUP = "force_setup";
    public static final int SETUP_VERSION = 4;

    private static final String DEFAULT_URL = "http://192.168.1.10:5000";

    private EditText input;
    private AlertDialog dialog;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(new FrameLayout(this));

        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String savedUrl = prefs.getString(PREF_SERVER_URL, "");
        int savedVersion = prefs.getInt(PREF_SETUP_VERSION, 0);
        boolean forceSetup = getIntent().getBooleanExtra(EXTRA_FORCE_SETUP, false);

        if (!forceSetup && savedVersion == SETUP_VERSION && savedUrl != null && !savedUrl.isEmpty()) {
            openMain();
            return;
        }

        input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(savedUrl != null && !savedUrl.isEmpty() ? savedUrl : DEFAULT_URL);
        input.setSelection(input.getText().length());

        dialog = new AlertDialog.Builder(this)
                .setTitle("Адрес сервера InTrAvia")
                .setMessage("Введите адрес сервера. Для Cloudflare Tunnel укажите полную HTTPS-ссылку, например https://example.trycloudflare.com")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("СОХРАНИТЬ", null)
                .setNegativeButton("ОТМЕНА", (d, w) -> cancelSetup())
                .create();

        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(v -> submit(prefs)));

        dialog.show();
    }

    private void cancelSetup() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String saved = prefs.getString(PREF_SERVER_URL, "");
        if (saved != null && !saved.isEmpty()) {
            dialog.dismiss();
            openMain();
        } else {
            finishAffinity();
        }
    }

    private void submit(SharedPreferences prefs) {
        final String url = normalize(input.getText().toString());
        if (url.isEmpty()) {
            input.setError("Введите адрес сервера, например https://example.trycloudflare.com");
            return;
        }
        if (!url.matches("(?i)^https?://[^\\s/]+.*$")) {
            input.setError("Некорректный адрес сервера");
            return;
        }

        final String checkUrl = url;
        final android.widget.Button saveButton = dialog.getButton(AlertDialog.BUTTON_POSITIVE);
        saveButton.setEnabled(false);
        saveButton.setText("ПРОВЕРКА…");
        input.setError(null);

        new Thread(() -> {
            String error = null;
            HttpURLConnection conn = null;
            try {
                URL target = new URL(checkUrl);
                conn = (HttpURLConnection) target.openConnection();
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(8000);
                conn.setInstanceFollowRedirects(true);
                conn.setRequestMethod("GET");
                int code = conn.getResponseCode();
                if (code < 200 || code >= 400) {
                    error = "Сервер ответил HTTP " + code + ". Проверьте адрес сервера.";
                }
            } catch (Exception e) {
                error = "Не удалось подключиться к серверу. Проверьте адрес, сеть и брандмауэр.";
            } finally {
                if (conn != null) conn.disconnect();
            }

            final String errorText = error;
            runOnUiThread(() -> {
                saveButton.setEnabled(true);
                saveButton.setText("СОХРАНИТЬ");
                if (errorText != null) {
                    input.setError(errorText);
                    return;
                }
                prefs.edit()
                        .putString(PREF_SERVER_URL, url)
                        .putInt(PREF_SETUP_VERSION, SETUP_VERSION)
                        .apply();
                dialog.dismiss();
                openMain();
            });
        }).start();
    }

    private String normalize(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) return "";
        if (!value.matches("(?i)^https?://.*")) {
            // Для IP/локальных серверов оставляем HTTP, для доменов (включая Cloudflare Tunnel) — HTTPS.
            boolean localAddress = value.matches("(?i)^(localhost|127(?:\\.\\d{1,3}){3}|10(?:\\.\\d{1,3}){3}|192\\.168(?:\\.\\d{1,3}){2}|172\\.(1[6-9]|2[0-9]|3[0-1])(?:\\.\\d{1,3}){2})(:\\d+)?(/.*)?$");
            value = (localAddress ? "http://" : "https://") + value;
        }
        value = value.replaceAll("/+$", "");
        value = value.replaceAll("(?i)/m/engineer$", "");
        value = value.replaceAll("(?i)/mobile$", "");
        value = value.replaceAll("(?i)/api$", "");
        value = value.replaceAll("(?i)/login\\.html$", "");
        return value;
    }

    private void openMain() {
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }
}
