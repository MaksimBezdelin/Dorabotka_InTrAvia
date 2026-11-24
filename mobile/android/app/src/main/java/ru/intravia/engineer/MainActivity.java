package ru.intravia.engineer;

import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

/**
 * Основная активность: открывает мобильные страницы с сервера,
 * адрес которого введён на стартовом нативном экране.
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        SharedPreferences prefs = getSharedPreferences(ServerSetupActivity.PREFS_NAME, MODE_PRIVATE);
        String serverUrl = prefs.getString(ServerSetupActivity.PREF_SERVER_URL, "");
        if (serverUrl == null || serverUrl.isEmpty()) {
            Intent intent = new Intent(this, ServerSetupActivity.class);
            intent.putExtra(ServerSetupActivity.EXTRA_FORCE_SETUP, true);
            startActivity(intent);
            finish();
            return;
        }

        WebView webView = getBridge().getWebView();
        webView.addJavascriptInterface(new ServerBridge(), "AndroidServer");
        webView.post(() -> webView.loadUrl(buildStartUrl(serverUrl)));
    }

    private String buildStartUrl(String serverUrl) {
        // Экран и Capacitor bridge остаются на локальном HTTPS origin.
        // API направляется на введённый сервер через server-config.js.
        return "https://localhost/login.html";
    }

    @Override
    public void onBackPressed() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    private class ServerBridge {
        @JavascriptInterface
        public String getServerUrl() {
            SharedPreferences prefs = getSharedPreferences(ServerSetupActivity.PREFS_NAME, MODE_PRIVATE);
            String value = prefs.getString(ServerSetupActivity.PREF_SERVER_URL, "");
            return value == null ? "" : value;
        }

        @JavascriptInterface
        public void changeServer() {
            runOnUiThread(() -> {
                // Старый адрес сохраняем: при «Отмене» вернёмся к нему.
                Intent intent = new Intent(MainActivity.this, ServerSetupActivity.class);
                intent.putExtra(ServerSetupActivity.EXTRA_FORCE_SETUP, true);
                startActivity(intent);
                finish();
            });
        }
    }
}
