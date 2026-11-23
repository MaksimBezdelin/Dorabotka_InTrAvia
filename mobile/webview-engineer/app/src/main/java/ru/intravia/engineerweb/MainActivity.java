package ru.intravia.engineerweb;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.GeolocationPermissions;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

/**
 * WebView-оболочка мобильного кабинета инженера InTrAvia.
 *
 * Приложение не хранит копию интерфейса: оно открывает /m/engineer с сервера,
 * поэтому любые обновления на сервере сразу видны на устройстве. Адрес сервера
 * задаётся при первом запуске и хранится в SharedPreferences.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "intravia_webview";
    private static final String KEY_BASE = "base_url";
    private static final String APP_PATH = "/m/engineer";
    private static final int LOCATION_REQUEST = 41;

    private WebView webView;
    private String baseUrl = "";
    private boolean loaded = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SharedPreferences preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
        baseUrl = preferences.getString(KEY_BASE, "");
        buildWebView();
        if (baseUrl.isEmpty()) {
            showServerDialog(true);
        } else {
            ensureLocationThenLoad();
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private void buildWebView() {
        FrameLayout root = new FrameLayout(this);
        webView = new WebView(this);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setGeolocationEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setSavePassword(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        settings.setUserAgentString(settings.getUserAgentString() + " InTrAviaWebView/1.0");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return openExternallyIfNeeded(request.getUrl());
            }

            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return openExternallyIfNeeded(Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                loaded = true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback callback) {
                callback.invoke(origin, true, false);
            }
        });
    }

    private boolean openExternallyIfNeeded(Uri uri) {
        if (uri == null) return false;
        String targetHost = Uri.parse(baseUrl).getHost();
        if (targetHost != null && targetHost.equalsIgnoreCase(uri.getHost())) {
            return false;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
            Toast.makeText(this, "Не удалось открыть ссылку", Toast.LENGTH_SHORT).show();
        }
        return true;
    }

    private void ensureLocationThenLoad() {
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
            }, LOCATION_REQUEST);
        } else {
            loadApp();
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == LOCATION_REQUEST) {
            // Даже без GPS интерфейс остаётся работоспособным: точку можно задать вручную.
            loadApp();
        }
    }

    private void loadApp() {
        if (baseUrl.isEmpty()) return;
        loaded = false;
        webView.loadUrl(baseUrl + APP_PATH);
    }

    private void showServerDialog(boolean firstRun) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("http://192.168.1.10:5000");
        input.setText(baseUrl.isEmpty() ? "http://10.0.2.2:5000" : baseUrl);

        AlertDialog.Builder builder = new AlertDialog.Builder(this)
                .setTitle("Адрес сервера InTrAvia")
                .setMessage("Базовый URL без /m/engineer. Например: http://192.168.1.10:5000")
                .setView(input)
                .setPositiveButton("Сохранить", null)
                .setNegativeButton("Отмена", null)
                .setCancelable(false);
        final AlertDialog dialog = builder.create();
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                .setOnClickListener(v -> {
                    String normalized = normalizeBase(input.getText().toString());
                    if (normalized == null) {
                        input.setError("Нужен адрес вида http://host:port");
                        return;
                    }
                    baseUrl = normalized;
                    prefs().edit().putString(KEY_BASE, baseUrl).apply();
                    dialog.dismiss();
                    ensureLocationThenLoad();
                }));
        dialog.setOnDismissListener(d -> {
            if (firstRun && baseUrl.isEmpty()) {
                finish();
            }
        });
        dialog.show();
    }

    private String normalizeBase(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) return null;
        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            value = "http://" + value;
        }
        while (value.endsWith("/")) value = value.substring(0, value.length() - 1);
        Uri uri = Uri.parse(value);
        if (uri.getHost() == null || uri.getHost().isEmpty()) return null;
        return value;
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Сервер");
        menu.add(0, 2, 1, "Обновить");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            showServerDialog(false);
            return true;
        }
        if (item.getItemId() == 2) {
            if (loaded) webView.reload(); else loadApp();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onPause() {
        if (webView != null) webView.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
