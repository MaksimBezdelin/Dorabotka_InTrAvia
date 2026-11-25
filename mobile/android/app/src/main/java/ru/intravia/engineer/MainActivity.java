package ru.intravia.engineer;

import android.content.Intent;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.hardware.GeomagneticField;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.view.Surface;

import java.util.Locale;

import com.getcapacitor.BridgeActivity;

/**
 * Основная активность: открывает мобильные страницы с сервера,
 * адрес которого введён на стартовом нативном экране.
 */
public class MainActivity extends BridgeActivity {

    private CompassBridge compassBridge;
    private TextToSpeech textToSpeech;
    private volatile boolean ttsReady = false;
    private volatile String pendingSpeech = null;

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
        compassBridge = new CompassBridge();
        webView.addJavascriptInterface(compassBridge, "AndroidCompass");
        webView.addJavascriptInterface(new VoiceBridge(), "AndroidVoice");
        textToSpeech = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int result = textToSpeech.setLanguage(new Locale("ru", "RU"));
                ttsReady = result != TextToSpeech.LANG_MISSING_DATA &&
                        result != TextToSpeech.LANG_NOT_SUPPORTED;
                if (ttsReady) {
                    textToSpeech.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build());
                }
                String queued = pendingSpeech;
                pendingSpeech = null;
                if (ttsReady && queued != null && !queued.isEmpty()) {
                    speakNative(queued);
                }
            }
        });
        webView.post(() -> webView.loadUrl(buildStartUrl(serverUrl)));
    }

    private String buildStartUrl(String serverUrl) {
        // Экран и Capacitor bridge остаются на локальном HTTPS origin.
        // API направляется на введённый сервер через server-config.js.
        return "https://localhost/login.html";
    }

    @Override
    public void onDestroy() {
        if (compassBridge != null) compassBridge.stop();
        if (textToSpeech != null) {
            textToSpeech.stop();
            textToSpeech.shutdown();
            textToSpeech = null;
        }
        super.onDestroy();
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

    /**
     * Native Android TTS for turn-by-turn navigation hints.
     * JavaScript calls AndroidVoice.speak(text); browser speechSynthesis is used
     * as a fallback when the page runs outside the Android shell.
     */
    private void speakNative(String value) {
        Bundle params = new Bundle();
        params.putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, 1.0f);
        textToSpeech.speak(value, TextToSpeech.QUEUE_FLUSH, params, "intravia-nav");
    }

    private class VoiceBridge {
        @JavascriptInterface
        public void speak(String text) {
            if (text == null) return;
            final String value = text.trim();
            if (value.isEmpty()) return;
            runOnUiThread(() -> {
                if (ttsReady) {
                    speakNative(value);
                } else {
                    pendingSpeech = value;
                }
            });
        }

        @JavascriptInterface
        public void stop() {
            runOnUiThread(() -> {
                if (textToSpeech != null) textToSpeech.stop();
            });
        }
    }

    /**
     * North-referenced native compass.
     * Use TYPE_ROTATION_VECTOR only when a physical gyroscope is present. Android
     * fuses gyroscope + accelerometer + magnetometer for fast turns with an absolute
     * north reference. Phones without this gyro-based sensor use GPS course in JS.
     *
     * The magnetic azimuth is corrected with GeomagneticField declination when a
     * GPS fix is available, so the heading sent to the map is relative to true north.
     */
    private class CompassBridge implements SensorEventListener {
        private final SensorManager sensorManager =
                (SensorManager) getSystemService(SENSOR_SERVICE);
        private final Handler mainHandler = new Handler(Looper.getMainLooper());
        private final Sensor rotationVectorSensor;
        private final Sensor gyroscopeSensor;
        private final Sensor accelerometerSensor;
        private final Sensor magnetometerSensor;
        private final float[] gravity = new float[3];
        private final float[] magnetic = new float[3];
        private final float[] rotation = new float[9];
        private final float[] adjustedRotation = new float[9];
        private final float[] inclination = new float[9];
        private final float[] orientation = new float[3];
        private boolean hasGravity;
        private boolean hasMagnetic;
        private boolean listening;
        private boolean usingRotationVector;
        private long lastCallbackAt;
        private float lastHeading = Float.NaN;
        private volatile boolean hasGpsLocation;
        private volatile float gpsLatitude;
        private volatile float gpsLongitude;
        private volatile float gpsAltitude;
        private volatile float magneticDeclination;

        CompassBridge() {
            rotationVectorSensor = sensorManager != null
                    ? sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR) : null;
            gyroscopeSensor = sensorManager != null
                    ? sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE) : null;
            accelerometerSensor = sensorManager != null
                    ? sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) : null;
            magnetometerSensor = sensorManager != null
                    ? sensorManager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD) : null;
        }

        @JavascriptInterface
        public boolean hasGyroscope() {
            return sensorManager != null && gyroscopeSensor != null;
        }

        @JavascriptInterface
        public boolean canUseGyroHeading() {
            // Android's rotation-vector sensor fuses the physical gyroscope with
            // accelerometer/magnetic reference to keep an absolute north heading.
            return hasGyroscope() && rotationVectorSensor != null;
        }

        @JavascriptInterface
        public boolean hasSensors() {
            return canUseGyroHeading();
        }

        @JavascriptInterface
        public String getMode() {
            return "Гироскоп Android + коррекция по северу";
        }

        /** Called from the mobile page after each fresh GPS fix to correct magnetic north to true north. */
        @JavascriptInterface
        public void setLocation(double latitude, double longitude, double altitude) {
            if (Double.isNaN(latitude) || Double.isInfinite(latitude)
                    || Double.isNaN(longitude) || Double.isInfinite(longitude)) return;
            if (latitude < -90.0 || latitude > 90.0 || longitude < -180.0 || longitude > 180.0) return;
            gpsLatitude = (float) latitude;
            gpsLongitude = (float) longitude;
            gpsAltitude = Double.isNaN(altitude) || Double.isInfinite(altitude)
                    ? 0.0f : (float) altitude;
            hasGpsLocation = true;
            try {
                GeomagneticField field = new GeomagneticField(
                        gpsLatitude, gpsLongitude, gpsAltitude, System.currentTimeMillis());
                magneticDeclination = field.getDeclination();
            } catch (Exception ignored) {
                magneticDeclination = 0.0f;
            }
        }

        @JavascriptInterface
        public boolean start() {
            // If there is no physical gyroscope (or Android cannot expose its fused
            // north-referenced rotation vector), JavaScript switches to GPS course.
            if (sensorManager == null || !canUseGyroHeading()) return false;
            if (listening) return true;

            lastCallbackAt = 0L;
            lastHeading = Float.NaN;
            usingRotationVector = true;

            // TYPE_ROTATION_VECTOR provides the fast gyro response and corrects
            // long-term drift against the magnetic north reference.
            listening = sensorManager.registerListener(
                    this, rotationVectorSensor, SensorManager.SENSOR_DELAY_GAME, mainHandler);
            if (!listening) sensorManager.unregisterListener(this);
            return listening;
        }

        @JavascriptInterface
        public void stop() {
            listening = false;
            if (sensorManager != null) sensorManager.unregisterListener(this);
        }

        private boolean updateRotationMatrix(SensorEvent event) {
            if (usingRotationVector && event.sensor.getType() == Sensor.TYPE_ROTATION_VECTOR) {
                SensorManager.getRotationMatrixFromVector(rotation, event.values);
                return true;
            }
            return false;
        }

        @Override
        public void onSensorChanged(SensorEvent event) {
            if (!listening || event == null || event.values == null) return;
            if (!updateRotationMatrix(event)) return;

            int displayRotation = getWindowManager().getDefaultDisplay().getRotation();
            int axisX = SensorManager.AXIS_X;
            int axisY = SensorManager.AXIS_Y;
            switch (displayRotation) {
                case Surface.ROTATION_90:
                    axisX = SensorManager.AXIS_Y;
                    axisY = SensorManager.AXIS_MINUS_X;
                    break;
                case Surface.ROTATION_180:
                    axisX = SensorManager.AXIS_MINUS_X;
                    axisY = SensorManager.AXIS_MINUS_Y;
                    break;
                case Surface.ROTATION_270:
                    axisX = SensorManager.AXIS_MINUS_Y;
                    axisY = SensorManager.AXIS_X;
                    break;
                case Surface.ROTATION_0:
                default:
                    break;
            }
            if (!SensorManager.remapCoordinateSystem(rotation, axisX, axisY, adjustedRotation)) return;
            SensorManager.getOrientation(adjustedRotation, orientation);

            // SensorManager's azimuth is magnetic-north referenced. Add local
            // geomagnetic declination to express the result relative to true north.
            float heading = (float) Math.toDegrees(orientation[0]) + magneticDeclination;
            heading = (heading % 360.0f + 360.0f) % 360.0f;

            long now = System.currentTimeMillis();
            float delta = Float.isNaN(lastHeading) ? 360.0f
                    : Math.abs(((heading - lastHeading + 540.0f) % 360.0f) - 180.0f);
            // Forward turns promptly; suppress only near-identical sensor noise.
            if (now - lastCallbackAt < 24L && delta < 0.35f) return;
            lastCallbackAt = now;
            lastHeading = heading;
            final String headingValue = String.format(Locale.US, "%.2f", heading);
            runOnUiThread(() -> {
                WebView webView = getBridge() != null ? getBridge().getWebView() : null;
                if (webView != null) {
                    webView.evaluateJavascript(
                            "window.onNativeCompassHeading && window.onNativeCompassHeading(" + headingValue + ");",
                            null);
                }
            });
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {
            if (sensor != null
                    && (sensor.getType() == Sensor.TYPE_MAGNETIC_FIELD
                        || sensor.getType() == Sensor.TYPE_ROTATION_VECTOR)
                    && accuracy == SensorManager.SENSOR_STATUS_UNRELIABLE) {
                runOnUiThread(() -> {
                    WebView webView = getBridge() != null ? getBridge().getWebView() : null;
                    if (webView != null) {
                        webView.evaluateJavascript(
                                "window.onNativeCompassAccuracy && window.onNativeCompassAccuracy('unreliable');",
                                null);
                    }
                });
            }
        }
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
