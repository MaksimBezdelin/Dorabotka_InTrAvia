/* InTrAvia — настройка адреса сервера для мобильного приложения */
(function () {
    var STORAGE_KEY = 'intraviaServerUrl';
    var COOKIE_KEY = 'intravia_server_url';

    function normalize(url) {
        var value = String(url == null ? '' : url).trim();
        if (!value) return '';
        if (!/^https?:\/\//i.test(value)) value = 'http://' + value;
        return value.replace(/\/+$/, '');
    }

    function getCookieServerUrl() {
        try {
            var match = document.cookie.match(new RegExp('(?:^|;\\s*)' + COOKIE_KEY + '=([^;]*)'));
            if (!match) return '';
            return normalize(decodeURIComponent(match[1]));
        } catch (_) {
            return '';
        }
    }

    function isCapacitorLocalPage() {
        try {
            return location.protocol === 'https:' &&
                location.hostname === 'localhost' &&
                (!location.port || location.port === '443');
        } catch (_) {
            return false;
        }
    }

    function getServerUrl() {
        // Если мобильные страницы раздал сам сервер (http://host:5000/mobile/...),
        // работаем с этим же origin — API доступен по относительному /api.
        try {
            if (!isCapacitorLocalPage() && (location.protocol === 'http:' || location.protocol === 'https:')) {
                return normalize(location.origin);
            }
        } catch (_) {}

        // Capacitor: нативный Android-мост читает адрес, сохранённый в SharedPreferences.
        try {
            if (window.AndroidServer && typeof window.AndroidServer.getServerUrl === 'function') {
                var nativeUrl = normalize(window.AndroidServer.getServerUrl());
                if (nativeUrl) return nativeUrl;
            }
        } catch (_) {}

        // Локальные страницы Capacitor: резервно адрес приходит из cookie/localStorage.
        var fromCookie = getCookieServerUrl();
        if (fromCookie) return fromCookie;
        try {
            return normalize(localStorage.getItem(STORAGE_KEY) || '');
        } catch (_) {
            return '';
        }
    }

    function setServerUrl(url) {
        var value = normalize(url);
        try {
            if (value) localStorage.setItem(STORAGE_KEY, value);
            else localStorage.removeItem(STORAGE_KEY);
        } catch (_) {}
        return value;
    }

    function apiUrl(path) {
        var base = getServerUrl();
        if (!base) throw new Error('Не указан адрес сервера');
        var suffix = String(path || '');
        if (suffix.charAt(0) !== '/') suffix = '/' + suffix;
        if (suffix.indexOf('/api/') !== 0 && suffix !== '/api') suffix = '/api' + suffix;
        return base + suffix;
    }


    // В локальном Capacitor WebView HTML загружается с https://localhost,
    // а API обслуживается удалённым сервером через HTTPS Cloudflare Tunnel.
    // Перенаправляем только /api; локальные /static остаются в приложении.
    if (!window.__intraviaFetchPatched && typeof window.fetch === 'function') {
        window.__intraviaFetchPatched = true;
        var originalFetch = window.fetch.bind(window);
        window.fetch = function (input, init) {
            var rawUrl = typeof input === 'string' ? input : (input && input.url);
            if (typeof rawUrl === 'string' && /^\/api(?:\/|\?|$)/i.test(rawUrl)) {
                try {
                    var target = getServerUrl() + rawUrl;
                    if (typeof input === 'string') input = target;
                    else input = new Request(target, input);
                } catch (error) {
                    return Promise.reject(error);
                }
            }
            return originalFetch(input, init);
        };
    }

    window.IntrAvia = window.IntrAvia || {};
    window.IntrAvia.serverUrl = getServerUrl;
    window.IntrAvia.setServerUrl = setServerUrl;
    window.IntrAvia.normalizeServerUrl = normalize;
    window.IntrAvia.apiUrl = apiUrl;
})();
