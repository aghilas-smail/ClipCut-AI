    // ── State ──────────────────────────────────────────────────────────────
    let currentJobId      = null;
    let pollInterval      = null;
    let logInterval       = null;
    let etaInterval       = null;
    let logOffset         = 0;
    let consoleOpen       = false;
    let playerClipIdx     = null;
    let trimClipIdx       = null;
    let isBatchMode       = false;
    let isUploadMode      = false;
    let batchJobIds       = [];
    let uploadedVideoPath = null;   // path returned by /api/upload
    // ETA state
    let etaTotalSec    = 0;
    let etaStartedAt   = 0;   // unix ms

    // ── Pre-generation estimate ────────────────────────────────────────────
    let estimateTimer = null;

    const _VIDEO_HOSTS = ['youtu','twitch.tv','kick.com','nimo.tv','tiktok.com',
                          'instagram.com','twitter.com','x.com','facebook.com',
                          'dailymotion.com','vimeo.com'];

    function isVideoUrl(url) {
      return _VIDEO_HOSTS.some(h => url.includes(h));
    }

    function scheduleEstimate() {
      clearTimeout(estimateTimer);
      const url  = document.getElementById('yt-url').value.trim();
      const card = document.getElementById('estimate-card');
      if (!url || !isVideoUrl(url)) {
        card.style.display = 'none';
        return;
      }
      estimateTimer = setTimeout(() => fetchEstimate(url), 700);
    }

    async function fetchEstimate(url) {
      const card    = document.getElementById('estimate-card');
      const loading = document.getElementById('estimate-loading');
      const result  = document.getElementById('estimate-result');
      const errEl   = document.getElementById('estimate-error');

      card.style.display = 'block';
      loading.style.display = 'flex';
      result.style.display  = 'none';
      errEl.style.display   = 'none';

      const whisperModel = document.getElementById('whisper-model')?.value || 'base';
      const maxClips     = parseInt(document.getElementById('max-clips').value) || 5;
      const clipDuration = parseInt(document.getElementById('clip-duration').value) || 60;

      try {
        const params = new URLSearchParams({
          url, whisper_model: whisperModel,
          max_clips: maxClips, clip_duration: clipDuration,
        });
        const res  = await fetch(`/api/estimate?${params}`);
        const data = await res.json();

        if (!res.ok) throw new Error(data.detail || 'Erreur analyse');

        // Fill result
        const thumb = document.getElementById('est-thumb');
        if (data.thumbnail) { thumb.src = data.thumbnail; thumb.style.display = ''; }
        else                 { thumb.style.display = 'none'; }

        document.getElementById('est-title').innerHTML =
          escHtml(data.title || 'Vidéo YouTube') +
          (data.cached ? '<span class="est-cache-badge">✓ En cache</span>' : '');

        const durMin = Math.floor(data.duration / 60);
        const durSec = data.duration % 60;
        const durStr = durMin > 0 ? `${durMin}min ${durSec}s` : `${durSec}s`;
        document.getElementById('est-sub').textContent =
          `${data.uploader || ''}  ·  ${durStr}  ·  Whisper ${data.whisper_model}`;

        document.getElementById('est-eta').textContent = `~${data.eta_fmt}`;

        // Heatmap badge — show when YouTube Most Replayed data is available
        const heatmapEl = document.getElementById('est-heatmap-badge');
        if (data.heatmap_available && data.hot_duration > 0) {
          const pct = data.duration > 0
            ? Math.round(data.hot_duration / data.duration * 100)
            : 0;
          heatmapEl.innerHTML =
            `🔥 Heatmap YouTube disponible — transcription intelligente activée ` +
            `(${data.hot_seg_count} zone${data.hot_seg_count > 1 ? 's' : ''} populaire${data.hot_seg_count > 1 ? 's' : ''}, ` +
            `${data.hot_duration_fmt} / ${pct}% de la vidéo)`;
          heatmapEl.style.display = 'flex';
        } else {
          heatmapEl.style.display = 'none';
        }

        const bd = data.breakdown;
        const transcribeLbl = data.heatmap_available && data.hot_duration > 0
          ? `🔥 Transcription (heatmap, ${data.hot_duration_fmt})`
          : '🎙️ Transcription (vidéo entière)';
        document.getElementById('est-breakdown').innerHTML = [
          { lbl: data.cached ? '📦 Cache' : '⬇️ Téléchargement', val: fmtSec(bd.download) },
          { lbl: transcribeLbl,                                    val: fmtSec(bd.transcribe) },
          { lbl: '🤖 Analyse IA',    val: fmtSec(bd.gpt)       },
          { lbl: '🎬 Rendu clips',   val: fmtSec(bd.ffmpeg)    },
        ].map(c => `
          <div class="est-chunk">
            <div class="est-chunk-val">${c.val}</div>
            <div class="est-chunk-lbl">${c.lbl}</div>
          </div>`).join('');

        loading.style.display = 'none';
        result.style.display  = 'block';
      } catch (e) {
        loading.style.display = 'none';
        errEl.style.display   = 'block';
        errEl.textContent     = '⚠️ ' + e.message;
      }
    }

    function escHtml(s) {
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
              .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // Re-run estimate when model/clips/duration changes
    function onParamChange() {
      const url = document.getElementById('yt-url').value.trim();
      if (url && url.includes('youtu')) fetchEstimate(url);
    }

    // ── Notifications ──────────────────────────────────────────────────────
    function requestNotifPermission() {
      if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
      }
    }

    function sendNotification(title, body) {
      if ('Notification' in window && Notification.permission === 'granted') {
        const n = new Notification(title, {
          body,
          icon: 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>✂️</text></svg>',
          badge: 'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>✂️</text></svg>',
        });
        n.onclick = () => { window.focus(); n.close(); };
      }
    }

    // ── ETA ────────────────────────────────────────────────────────────────
    function fmtSec(sec) {
      const s = Math.max(0, Math.round(sec));
      const m = Math.floor(s / 60);
      const r = s % 60;
      return m > 0 ? `${m}min ${String(r).padStart(2,'0')}s` : `${r}s`;
    }

    function startEtaCountdown(totalSec, startedAtUnix) {
      etaTotalSec  = totalSec;
      etaStartedAt = startedAtUnix * 1000;  // server sends unix timestamp (s → ms)
      const bar    = document.getElementById('eta-bar');
      bar.classList.add('visible');
      document.getElementById('eta-detail').textContent =
        `Durée totale estimée : ~${fmtSec(totalSec)}`;

      if (etaInterval) clearInterval(etaInterval);
      etaInterval = setInterval(() => {
        const elapsed   = (Date.now() - etaStartedAt) / 1000;
        const remaining = etaTotalSec - elapsed;
        const valEl     = document.getElementById('eta-value');
        if (remaining <= 0) {
          valEl.textContent = 'Finalisation…';
        } else {
          valEl.textContent = `~${fmtSec(remaining)}`;
        }
      }, 1000);
    }

    function stopEtaCountdown() {
      if (etaInterval) { clearInterval(etaInterval); etaInterval = null; }
      document.getElementById('eta-bar').classList.remove('visible');
    }

    // ── Views ──────────────────────────────────────────────────────────────
    function showView(v) {
      document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
      document.querySelector(`.nav-link[onclick="showView('${v}')"]`)?.classList.add('active');
      document.getElementById('main-card').style.display = v === 'main' ? '' : 'none';
      document.getElementById('main-features').style.display = v === 'main' ? '' : 'none';
      document.getElementById('history-section').style.display = v === 'history' ? '' : 'none';
      if (v === 'history') loadHistory();
    }

    // ── Visual preset selector ─────────────────────────────────────────────
    function selectVisual(el) {
      document.querySelectorAll('#visual-picker .style-card').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
    }

    // ── Mode switch (single / batch / upload) ──────────────────────────────
    function switchMode(m) {
      isBatchMode  = m === 'batch';
      isUploadMode = m === 'upload';
      ['single','batch','upload'].forEach(id => {
        document.getElementById('tab-' + id).classList.toggle('active', m === id);
      });
      document.getElementById('single-url-group').style.display = m === 'single' ? '' : 'none';
      document.getElementById('batch-url-group').style.display  = m === 'batch'  ? '' : 'none';
      document.getElementById('upload-group').style.display     = m === 'upload' ? '' : 'none';
      // Hide estimate card in upload mode
      document.getElementById('estimate-card').style.display    = m === 'single' ? '' : 'none';
    }

    // ── File upload helpers ────────────────────────────────────────────────
    function handleFileDrop(e) {
      e.preventDefault();
      document.getElementById('drop-zone').style.borderColor = 'var(--border)';
      const file = e.dataTransfer.files[0];
      if (file) handleFileSelect(file);
    }

    async function handleFileSelect(file) {
      if (!file) return;
      const statusEl   = document.getElementById('upload-status');
      const nameEl     = document.getElementById('upload-filename');
      const sizeEl     = document.getElementById('upload-size');
      const progressEl = document.getElementById('upload-progress');

      nameEl.textContent     = file.name;
      sizeEl.textContent     = `(${(file.size / 1024 / 1024).toFixed(1)} Mo)`;
      progressEl.textContent = 'Upload en cours…';
      statusEl.style.display = 'block';
      uploadedVideoPath      = null;

      const formData = new FormData();
      formData.append('file', file);

      try {
        const res  = await fetch('/api/upload', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Erreur upload');
        uploadedVideoPath      = data.local_video_path;
        progressEl.textContent = `✅ Prêt (${data.size_mb} Mo)`;
        progressEl.style.color = 'var(--ok, #4ade80)';
      } catch (err) {
        progressEl.textContent = `❌ ${err.message}`;
        progressEl.style.color = 'var(--error, #f87171)';
      }
    }

    function addUrl() {
      const list = document.getElementById('url-list');
      const row  = document.createElement('div');
      row.className = 'url-row';
      row.innerHTML = `<input type="text" placeholder="https://www.youtube.com/watch?v=…" autocomplete="off" />
                       <button type="button" class="url-remove" onclick="removeUrl(this)">✕</button>`;
      list.appendChild(row);
    }

    function removeUrl(btn) {
      btn.closest('.url-row').remove();
    }

    // ── Console ────────────────────────────────────────────────────────────
    function toggleConsole() {
      consoleOpen = !consoleOpen;
      document.getElementById('console-box').classList.toggle('open', consoleOpen);
      document.getElementById('console-arrow').textContent = consoleOpen ? '▼' : '▶';
    }

    function appendLogs(lines) {
      const box = document.getElementById('console-box');
      lines.forEach(line => {
        const div = document.createElement('div');
        div.className = 'log-line' +
          (line.includes('✓') || line.includes('terminé') ? ' ok' : '') +
          (line.includes('⚠️') ? ' warn' : '') +
          (line.includes('GPT') || line.includes('Whisper') || line.includes('ffmpeg') ? ' info' : '');
        div.textContent = line;
        box.appendChild(div);
      });
      // Auto-scroll to bottom
      box.scrollTop = box.scrollHeight;
      // Update badge count
      document.getElementById('console-badge').textContent = logOffset;
    }

    async function pollLogs() {
      if (!currentJobId) return;
      try {
        const res  = await fetch(`/api/logs/${currentJobId}?since=${logOffset}`);
        const data = await res.json();
        if (data.logs && data.logs.length > 0) {
          logOffset += data.logs.length;
          document.getElementById('console-badge').textContent = logOffset;
          appendLogs(data.logs);
        }
      } catch { /* ignore */ }
    }

    function startLogPolling() {
      logOffset = 0;
      document.getElementById('console-box').innerHTML = '';
      // Auto-open console so users see it
      if (!consoleOpen) toggleConsole();
      if (logInterval) clearInterval(logInterval);
      logInterval = setInterval(pollLogs, 1500);
    }

    function stopLogPolling() {
      if (logInterval) { clearInterval(logInterval); logInterval = null; }
      // One last poll to capture final messages
      setTimeout(pollLogs, 800);
    }

    // ── Advanced toggle ────────────────────────────────────────────────────
    function toggleAdvanced() {
      const s = document.getElementById('advanced-section');
      const a = document.getElementById('adv-arrow');
      s.classList.toggle('open');
      a.textContent = s.classList.contains('open') ? '▼' : '▶';
    }

    // ── Helpers ────────────────────────────────────────────────────────────
    function toggleKey() {
      const el = document.getElementById('api-key');
      el.type = el.type === 'password' ? 'text' : 'password';
    }

    function selectStyle(el) {
      document.querySelectorAll('#style-picker .style-card').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
    }

    function selectSpeed(el) {
      document.querySelectorAll('#speed-picker .style-card').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
    }

    function parseTime(val) {
      if (!val || !val.trim()) return null;
      const v = val.trim();
      if (v.includes(':')) {
        const [m, s] = v.split(':').map(Number);
        return m * 60 + (s || 0);
      }
      const n = parseFloat(v);
      return isNaN(n) ? null : n;
    }

    // ── Start processing ───────────────────────────────────────────────────
    async function startProcessing() {
      const maxClips       = parseInt(document.getElementById('max-clips').value);
      const clipDuration   = parseInt(document.getElementById('clip-duration').value);
      const language       = document.getElementById('language').value;
      const subtitleStyle  = document.querySelector('#style-picker .style-card.active')?.dataset.style || 'elevate';
      const visualEnhance  = document.querySelector('#visual-picker .style-card.active')?.dataset.enhance || 'none';
      const speedFactor    = parseFloat(document.querySelector('#speed-picker .style-card.active')?.dataset.speed || '1');
      const subtitleLang   = document.getElementById('subtitle-lang').value;
      const enableSubtitles= document.getElementById('enable-subtitles').checked;
      const faceTracking   = document.getElementById('face-tracking').checked;
      const smartZoom      = document.getElementById('smart-zoom').checked;
      const silenceRemoval = document.getElementById('silence-removal').checked;
      const addHook        = document.getElementById('add-hook').checked;
      const videoStart     = parseTime(document.getElementById('video-start').value);
      const videoEnd       = parseTime(document.getElementById('video-end').value);
      const whisperModel   = document.getElementById('whisper-model').value;
      const watermark      = document.getElementById('watermark').value.trim();
      const musicTrack     = document.getElementById('music-track').value.trim();
      const musicVolume    = parseFloat(document.getElementById('music-volume').value);
      const webhookUrl     = document.getElementById('webhook-url').value.trim();

      const basePayload = {
        max_clips: maxClips, clip_duration: clipDuration,
        language, subtitle_style: subtitleStyle, subtitle_lang: subtitleLang,
        enable_subtitles: enableSubtitles,
        speed_factor: speedFactor,
        face_tracking: faceTracking, smart_zoom: smartZoom,
        silence_removal: silenceRemoval, add_hook: addHook,
        video_start: videoStart, video_end: videoEnd,
        whisper_model: whisperModel, watermark, music_track: musicTrack,
        music_volume: musicVolume, webhook_url: webhookUrl,
        visual_enhance: visualEnhance,
      };

      resetUI();
      setBusy(true);
      startLogPolling();

      try {
        if (isUploadMode) {
          // Upload mode
          if (!uploadedVideoPath) {
            setBusy(false);
            return alert('Veuillez d\'abord uploader une vidéo.');
          }
          const res  = await fetch('/api/process', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ local_video_path: uploadedVideoPath, ...basePayload }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || 'Erreur serveur');
          currentJobId = data.job_id;
          pollInterval = setInterval(pollStatus, 2500);

        } else if (isBatchMode) {
          // Batch URL mode
          const urls = Array.from(document.querySelectorAll('#url-list input'))
                            .map(i => i.value.trim()).filter(Boolean);
          if (!urls.length) { setBusy(false); return alert('Ajoutez au moins une URL.'); }
          const res  = await fetch('/api/process-batch', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ...basePayload, youtube_urls: urls }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || 'Erreur serveur');
          batchJobIds  = data.jobs.map(j => j.job_id);
          currentJobId = batchJobIds[0];
          setProgress(5, `Batch lancé : ${batchJobIds.length} vidéo(s) en traitement…`);
          pollInterval = setInterval(() => pollBatch(batchJobIds), 3000);

        } else {
          // Single URL mode
          const url = document.getElementById('yt-url').value.trim();
          if (!url) { setBusy(false); return alert('Veuillez coller une URL.'); }
          const res  = await fetch('/api/process', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ youtube_url: url, ...basePayload }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || 'Erreur serveur');
          currentJobId = data.job_id;
          pollInterval = setInterval(pollStatus, 2500);
        }
      } catch (err) {
        showError(err.message);
        setBusy(false);
      }
    }

    // ── Poll (single) ──────────────────────────────────────────────────────
    async function pollStatus() {
      if (!currentJobId) return;
      try {
        const res = await fetch(`/api/status/${currentJobId}`);
        const job = await res.json();
        setProgress(job.progress, job.message);

        // Start ETA countdown as soon as the server has calculated it
        if (job.eta_seconds && !etaInterval) {
          startEtaCountdown(job.eta_seconds, job.started_at);
        }

        if (job.status === 'completed') {
          clearInterval(pollInterval);
          stopLogPolling();
          stopEtaCountdown();
          showResults(job);
          setBusy(false);
          sendNotification(
            '✂️ ClipCut AI — Clips prêts !',
            `${job.clips.length} clip(s) TikTok sont prêts à télécharger.`
          );
        } else if (job.status === 'error') {
          clearInterval(pollInterval);
          stopLogPolling();
          stopEtaCountdown();
          showError(job.error || 'Erreur inconnue');
          setBusy(false);
          sendNotification('ClipCut AI — Erreur', job.error || 'Le traitement a échoué.');
        }
      } catch {
        clearInterval(pollInterval);
        showError('Impossible de contacter le serveur.');
        setBusy(false);
      }
    }

    // ── Poll (batch) ──────────────────────────────────────────────────────
    async function pollBatch(jobIds) {
      const statuses = await Promise.all(jobIds.map(id =>
        fetch(`/api/status/${id}`).then(r => r.json()).catch(() => ({status:'error'}))
      ));
      const done     = statuses.filter(j => j.status === 'completed').length;
      const total    = jobIds.length;
      const errored  = statuses.filter(j => j.status === 'error').length;
      setProgress(Math.round(done/total*100),
                  `Batch : ${done}/${total} vidéo(s) terminée(s)${errored?' ('+errored+' erreur(s))':''}`);
      if (done + errored === total) {
        clearInterval(pollInterval);
        // Show results from first completed job
        const firstDone = statuses.findIndex(j => j.status === 'completed');
        if (firstDone >= 0) {
          currentJobId = jobIds[firstDone];
          showResults(statuses[firstDone]);
        }
        setBusy(false);
      }
    }

    // ── UI helpers ─────────────────────────────────────────────────────────
    function resetUI() {
      // Reset job state so stale job_id / logOffset don't bleed into new run
      currentJobId = null;
      logOffset    = 0;
      if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
      if (logInterval)  { clearInterval(logInterval);  logInterval  = null; }

      document.getElementById('error-box').style.display = 'none';
      document.getElementById('results-section').style.display = 'none';
      document.getElementById('progress-section').style.display = 'block';
      setProgress(0, 'Envoi de la requête…');
      stopEtaCountdown();
      etaInterval = null;
      document.getElementById('eta-value').textContent = '—';
      document.getElementById('eta-detail').textContent = 'Calcul en cours…';
    }

    function setBusy(busy) {
      const btn  = document.getElementById('submit-btn');
      const text = document.getElementById('btn-text');
      btn.disabled = busy;
      text.innerHTML = busy
        ? '<span class="spinner"></span> Traitement en cours…'
        : '✂️ Générer mes clips TikTok';
    }

    function setProgress(pct, msg) {
      if (pct !== null) {
        document.getElementById('progress-fill').style.width = pct + '%';
        document.getElementById('progress-pct').textContent = pct + '%';
      }
      document.getElementById('progress-msg').textContent = msg;
    }

    function showError(msg) {
      document.getElementById('progress-section').style.display = 'none';
      document.getElementById('error-msg').textContent = msg;
      document.getElementById('error-box').style.display = 'block';
    }

    // ── Show results ───────────────────────────────────────────────────────
    function showResults(job) {
      document.getElementById('progress-section').style.display = 'none';
      const grid = document.getElementById('clips-grid');
      document.getElementById('results-count').textContent = job.clips.length;
      grid.innerHTML = '';

      // Wire up log links
      if (currentJobId) {
        const viewLink = document.getElementById('view-logs-link');
        if (viewLink) viewLink.href = `/api/logs/${currentJobId}/file`;
      }

      job.clips.forEach((clip, i) => {
        const mins = Math.floor(clip.duration / 60);
        const secs = Math.round(clip.duration % 60);
        const dur  = mins > 0 ? `${mins}:${String(secs).padStart(2,'0')}` : `${secs}s`;
        const emojis = ['🔥','⚡','💡','🎯','✨','🚀','💎','🎬'];
        const emoji  = emojis[i % emojis.length];
        const safeTitle = clip.title.replace(/'/g, "\\'").replace(/"/g, '&quot;');
        const score     = clip.score || 0;
        const stars     = '★'.repeat(Math.min(score, 10)).slice(0,5);
        const caption   = (clip.caption || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const captionLines = caption.split('\n').filter(Boolean);
        const captionDisplay = captionLines.slice(0,2).join('<br>');

        grid.innerHTML += `
          <div class="clip-card" id="clip-card-${i}">
            <div class="clip-thumb" onclick="openPlayer(${i}, '${safeTitle}')">
              <img id="thumb-${i}" style="display:none;" alt="Clip ${i+1}">
              <span class="thumb-emoji">${emoji}</span>
              <span class="clip-duration">${dur}</span>
              ${score ? `<span class="clip-score">${stars} ${score}/10</span>` : ''}
              <div class="play-overlay">
                <div class="play-circle">▶</div>
              </div>
            </div>
            <div class="clip-info">
              <div class="clip-num">Clip ${i + 1}</div>
              <div class="clip-title">${clip.title.replace(/</g,'&lt;')}</div>
              <div class="clip-buttons">
                <button class="preview-btn" onclick="openPlayer(${i}, '${safeTitle}')">▶ Preview</button>
                <button class="dl-btn" onclick="downloadClip(${i})">⬇️</button>
                <button class="trim-btn" onclick="openTrim(${i})" title="Ajuster le découpage">✂️</button>
              </div>
              ${caption ? `
              <div class="caption-box">
                <button class="caption-copy" onclick="copyCaption(${i})" title="Copier la caption">📋</button>
                <div id="caption-text-${i}" style="font-size:.74rem;padding-right:22px;">${captionDisplay}</div>
              </div>` : ''}
            </div>
          </div>`;

        // Load thumbnail asynchronously
        loadThumb(i);
      });

      const warnBox = document.getElementById('warnings-box');
      if (job.warnings && job.warnings.length > 0) {
        warnBox.style.display = 'block';
        warnBox.textContent = '⚠️ ' + job.warnings.join('\n⚠️ ');
      } else {
        warnBox.style.display = 'none';
      }

      document.getElementById('results-section').style.display = 'block';
      document.getElementById('results-section')
              .scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function loadThumb(i) {
      const img = document.getElementById(`thumb-${i}`);
      if (!img || !currentJobId) return;
      const src = `/api/thumbnail/${currentJobId}/${i}`;
      const tmp = new Image();
      tmp.onload = () => {
        img.src = src;
        img.style.display = 'block';
        const emoji = img.closest('.clip-thumb')?.querySelector('.thumb-emoji');
        if (emoji) emoji.style.display = 'none';
      };
      tmp.onerror = () => {};  // keep emoji fallback
      tmp.src = src;
    }

    function copyCaption(i) {
      const el = document.getElementById(`caption-text-${i}`);
      if (!el) return;
      const text = el.innerText;
      navigator.clipboard.writeText(text).then(() => {
        const btn = el.closest('.caption-box')?.querySelector('.caption-copy');
        if (btn) { btn.textContent = '✅'; setTimeout(() => btn.textContent = '📋', 1500); }
      });
    }

    function downloadClip(index) {
      window.location.href = `/api/download/${currentJobId}/${index}`;
    }

    function downloadAll() {
      if (!currentJobId) return;
      const a = document.createElement('a');
      a.href = `/api/download-zip/${currentJobId}`;
      a.download = 'tiktok_clips.zip';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }

    function downloadLogs() {
      if (!currentJobId) return;
      const a = document.createElement('a');
      a.href = `/api/logs/${currentJobId}/file?download=true`;
      a.download = `clipcut_${currentJobId.slice(0,8)}.log`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }

    // ── Video player modal ─────────────────────────────────────────────────
    function openPlayer(index, title) {
      playerClipIdx = index;
      const vid = document.getElementById('player-video');
      vid.src = `/api/download/${currentJobId}/${index}`;
      vid.load();
      document.getElementById('player-clip-title').textContent = title || `Clip ${index + 1}`;
      document.getElementById('player-overlay').classList.add('open');
    }

    function closePlayer() {
      const vid = document.getElementById('player-video');
      vid.pause(); vid.src = '';
      document.getElementById('player-overlay').classList.remove('open');
    }

    function playerDownload() {
      if (playerClipIdx !== null) downloadClip(playerClipIdx);
    }

    // ── Trim modal ─────────────────────────────────────────────────────────
    function openTrim(index) {
      trimClipIdx = index;
      document.getElementById('trim-start-offset').value = 0;
      document.getElementById('trim-end-offset').value = 0;
      document.getElementById('trim-info').textContent =
        `Clip ${index + 1} — modifie les décalages en secondes (négatif = plus tôt, positif = plus tard).`;
      document.getElementById('trim-overlay').classList.add('open');
    }

    function closeTrim() {
      document.getElementById('trim-overlay').classList.remove('open');
      trimClipIdx = null;
    }

    async function applyTrim() {
      if (trimClipIdx === null || !currentJobId) return;
      const trimStart = parseFloat(document.getElementById('trim-start-offset').value) || 0;
      const trimEnd   = parseFloat(document.getElementById('trim-end-offset').value) || 0;
      const btn = document.querySelector('.trim-apply-btn');
      btn.textContent = 'Application…';
      btn.disabled = true;
      try {
        const res = await fetch(
          `/api/trim/${currentJobId}/${trimClipIdx}?trim_start=${trimStart}&trim_end=${trimEnd}`,
          { method: 'POST' }
        );
        if (res.ok) {
          closeTrim();
          // Refresh thumbnail after a delay
          setTimeout(() => loadThumb(trimClipIdx), 3000);
        } else {
          const d = await res.json();
          alert('Erreur : ' + (d.detail || 'trim échoué'));
        }
      } catch (e) {
        alert('Erreur réseau : ' + e.message);
      }
      btn.textContent = 'Appliquer le trim';
      btn.disabled = false;
    }

    // ── History ────────────────────────────────────────────────────────────
    async function loadHistory() {
      const list = document.getElementById('history-list');
      list.innerHTML = '<div style="color:var(--muted);font-size:.88rem;">Chargement…</div>';
      try {
        const res  = await fetch('/api/history?limit=20');
        const data = await res.json();
        if (!data.length) {
          list.innerHTML = '<div style="color:var(--muted);font-size:.88rem)">Aucun projet dans l\'historique.</div>';
          return;
        }
        list.innerHTML = data.map(row => `
          <div class="history-card" onclick="reloadJob('${row.id}')">
            <div>
              <div class="history-title">${(row.title || 'Sans titre').replace(/</g,'&lt;')}</div>
              <div class="history-meta">
                ${row.clip_count} clip(s) · ${(row.created_at||'').slice(0,16).replace('T',' ')} UTC
              </div>
            </div>
            <span class="history-badge ${row.status === 'completed' ? 'badge-done' : 'badge-error'}">
              ${row.status === 'completed' ? '✓ Terminé' : '✗ Erreur'}
            </span>
          </div>`).join('');
      } catch {
        list.innerHTML = '<div style="color:#fe6b85;">Impossible de charger l\'historique.</div>';
      }
    }

    function reloadJob(jobId) {
      currentJobId = jobId;
      showView('main');
      // Poll once to get clips
      fetch(`/api/status/${jobId}`)
        .then(r => r.json())
        .then(job => {
          if (job.status === 'completed' && job.clips?.length) {
            showResults(job);
          }
        });
    }

    // ── Init ───────────────────────────────────────────────────────────────
    // Ask for notification permission as soon as the page loads
    requestNotifPermission();

    // ── Keyboard / click listeners ─────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
      document.getElementById('player-overlay').addEventListener('click', function(e) {
        if (e.target === this) closePlayer();
      });
      document.getElementById('trim-overlay').addEventListener('click', function(e) {
        if (e.target === this) closeTrim();
      });
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') { closePlayer(); closeTrim(); }
    });
