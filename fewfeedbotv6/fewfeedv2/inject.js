window.fewfeed = { installed: true };

(function () {
    const _origJson = Response.prototype.json;
    Response.prototype.json = function () {
        const resp = this;
        const url = resp.url || '';
        const isGraphWrite = /graph\.facebook\.com\/.*\/(photos|videos)/i.test(url);

        if (!isGraphWrite) return _origJson.call(resp);

        return resp.clone().text().then(function (text) {
            try {
                return JSON.parse(text);
            } catch (e) {
                console.warn('[FewFeed] ⚠️ Graph API returned non-JSON for ' + url.substring(0, 80));
                console.warn('[FewFeed] Response body (first 200 chars):', text.substring(0, 200));
                return { id: 'waf_' + Date.now(), post_id: 'waf_' + Date.now() };
            }
        });
    };
})();

(function () {
    if (window._fwHookLoadedV7) return;
    window._fwHookLoadedV7 = true;

    function calcJ(d) {
        if (!d) return '';
        var s = 0;
        for (var i = 0; i < d.length; i++) s += d.charCodeAt(i);
        return '2' + s;
    }

    function genUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    function getUID() {
        var m = document.cookie.match(/fb_proxy_cookies=([^;]+)/);
        if (m) {
            var m2 = decodeURIComponent(m[1]).match(/c_user=(\d+)/);
            if (m2) return m2[1];
        }
        var s = localStorage.getItem('fewfeed_fb_id');
        if (s) return s;
        return '';
    }

    let savedPid = '';
    let _uploadContext = null;

    function processBody(body) {
        if (!(body instanceof FormData)) return body;

        let pairs = [];
        for (let p of body.entries()) pairs.push(p);

        for (let p of pairs) {
            if ((p[0] === 'target_id' || p[0] === 'av') && p[1] && p[1] !== 'undefined') savedPid = p[1];
        }

        for (let p of pairs) {
            if (p[0].startsWith(' ') || p[0].endsWith(' ')) {
                body.delete(p[0]);
                body.append(p[0].trim(), p[1]);
            }
        }

        if (body.has('composer_entry_point_ref')) {
            _uploadContext = body.get('composer_entry_point_ref');
            console.log('[FewFeed] 📹 Upload context set:', _uploadContext);
        }

        if (body.has('source') && body.get('source') === 'composer') {
            if (_uploadContext === 'group') {
                console.log('[FewFeed] Group video: keeping source=composer');
            } else {
                body.set('source', 'composer_cs_reel_composer');
            }
        }

        if (body.has('waterfall_id')) {
            let wid = body.get('waterfall_id');
            if (wid && !wid.includes('-')) {
                body.set('waterfall_id', genUUID());
            }
        }

        if (body.has('fb_dtsg') && !body.has('jazoest')) {
            body.append('jazoest', calcJ(body.get('fb_dtsg')));
        }
        if (!body.has('__a')) {
            body.append('__a', '1');
        }

        let uid = getUID();
        if (uid && !body.has('__user')) {
            body.append('__user', uid);
        }

        return body;
    }

    const _origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    const _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.send = function (body) {
        try { body = processBody(body); } catch (e) { console.error(e); }

        if (this._fwGroupVideo && this._fwMethod === 'POST') {
            try {
                _origSetHeader.call(this, 'Offset', '0');
                _origSetHeader.call(this, 'X-Entity-Length', body ? body.byteLength || body.length || body.size || 0 : 0);
                _origSetHeader.call(this, 'X-Entity-Type', 'video/mp4');
                console.log('[FewFeed] ✅ Injected Offset:0 + Entity headers for Group video upload');
            } catch (e) { console.warn('[FewFeed] Header inject warning:', e); }
        }

        return _origSend.apply(this, arguments);
    };

    const _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        this._fwGroupVideo = false;
        this._fwMethod = method;

        if (typeof url === 'string') {
            if (url.includes('/ajax/video/upload/requests/start')) {
                _uploadContext = null;
                console.log('[FewFeed] \ud83d\udd04 Upload context reset (new init upload)');
            }
            if (savedPid) {
                url = url.replace(/target_id=undefined/g, 'target_id=' + savedPid);
                url = url.replace(/&av=undefined/g, '&av=' + savedPid);
                url = url.replace(/\?av=undefined/g, '?av=' + savedPid);
            }
            if (url.includes('/fb_video/') && _uploadContext === 'group') {
                this._fwGroupVideo = true;
                if (url.includes('reel_video=')) {
                    url = url.replace('reel_video=', 'video_id=');
                    console.log('[FewFeed] ✅ XHR Patched: reel_video → video_id (Group context)');
                }
            }
        }
        return _origOpen.apply(this, [method, url].concat([].slice.call(arguments, 2)));
    };

    const _origFetch = window.fetch;
    window.fetch = function (input, init) {
        let url = typeof input === 'string' ? input : (input && input.url);

        if (init && init.body instanceof FormData) {
            try { init.body = processBody(init.body); } catch (e) { console.error(e); }
        }

        if (typeof url === 'string') {
            if (url.includes('/ajax/video/upload/requests/start')) {
                _uploadContext = null;
                console.log('[FewFeed] \ud83d\udd04 Fetch: Upload context reset (new init upload)');
            }

            let nu = url;
            if (savedPid) {
                nu = nu.replace(/target_id=undefined/g, 'target_id=' + savedPid);
                nu = nu.replace(/&av=undefined/g, '&av=' + savedPid);
                nu = nu.replace(/\?av=undefined/g, '?av=' + savedPid);
            }
            if (nu.includes('wall_reel_id=') && _uploadContext === 'group') {
                nu = nu.replace('wall_reel_id=', 'video_id=');
                console.log('[FewFeed] ✅ Fetch Patched: wall_reel_id → video_id (Group context)');
            }
            if (nu !== url) {
                if (typeof input === 'string') input = nu;
                else if (input && input.url) input = new Request(nu, input);
            }
        }

        return _origFetch.call(this, input, init);
    };
    console.log('[FewFeed Extension] 🔥 Ultimate Request Interceptor V8 activated (+ Group Video Fix)!');
})();

