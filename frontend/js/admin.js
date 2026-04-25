  async function loadStats() {
    try {
      const res  = await fetch('/api/admin/stats');
      const data = await res.json();
      const s    = data;

      document.getElementById('stats-grid').innerHTML = `
        <div class="stat-card">
          <div class="stat-label">Total jobs</div>
          <div class="stat-value">${s.jobs_total}</div>
          <div class="stat-sub">depuis le démarrage</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Jobs actifs</div>
          <div class="stat-value" style="color:var(--accent2)">${s.jobs_active}</div>
          <div class="stat-sub">en traitement</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Cache vidéo</div>
          <div class="stat-value">${s.cache_total_mb} MB</div>
          <div class="stat-sub">${s.cache_files.length} fichier(s)</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Clips générés</div>
          <div class="stat-value">${s.output_total_mb} MB</div>
          <div class="stat-sub">sur le disque</div>
        </div>
        ${Object.entries(s.jobs_by_status || {}).map(([k,v]) =>
          `<div class="stat-card"><div class="stat-label">${k}</div>
           <div class="stat-value">${v}</div></div>`
        ).join('')}
      `;

      // Cache table
      if (!s.cache_files.length) {
        document.getElementById('cache-list').innerHTML =
          '<div class="empty">Cache vide.</div>';
      } else {
        document.getElementById('cache-list').innerHTML = `
          <table>
            <tr><th>Fichier</th><th>Taille</th></tr>
            ${s.cache_files.map(f =>
              `<tr><td style="font-family:monospace;font-size:.8rem;">${f.name}</td>
                   <td>${f.size_mb} MB</td></tr>`
            ).join('')}
          </table>`;
      }

      // Active jobs (jobs with status 'processing')
      await loadJobsList();
      await loadWhisperCache();
      await loadTranscriptCache();

    } catch(e) {
      document.getElementById('stats-grid').innerHTML =
        `<div style="color:#fe6b85;font-size:.88rem;">Erreur : ${e.message}</div>`;
    }
  }

  async function loadWhisperCache() {
    try {
      const res  = await fetch('/api/admin/whisper-cache');
      const data = await res.json();
      const el   = document.getElementById('whisper-cache-info');
      if (data.count === 0) {
        el.innerHTML = '<div class="empty">Aucun modèle en cache — sera chargé au prochain job.</div>';
      } else {
        el.innerHTML = `
          <table>
            <tr><th>Modèle</th><th>Statut</th></tr>
            ${data.loaded_models.map(m => `
              <tr>
                <td style="font-family:monospace">${m}</td>
                <td><span class="badge badge-done">✓ En RAM</span></td>
              </tr>`).join('')}
          </table>
          <p style="font-size:.78rem;color:var(--muted);margin-top:10px;">
            Le modèle reste chargé entre les jobs — pas de rechargement à chaque génération.
          </p>`;
      }
    } catch(e) {
      document.getElementById('whisper-cache-info').innerHTML =
        `<div style="color:#fe6b85;">Erreur : ${e.message}</div>`;
    }
  }

  async function loadJobsList() {
    try {
      const res  = await fetch('/api/history?limit=20');
      const data = await res.json();

      const active = data.filter(j => j.status === 'processing');
      const recent = data;

      const renderRow = j => `
        <tr>
          <td style="font-family:monospace;font-size:.76rem;opacity:.7">${j.id.slice(0,8)}…</td>
          <td>${(j.title || 'Sans titre').slice(0,50).replace(/</g,'&lt;')}</td>
          <td><span class="badge ${j.status==='completed'?'badge-done':j.status==='processing'?'badge-proc':'badge-err'}">
            ${j.status}</span></td>
          <td>${j.clip_count} clip(s)</td>
          <td style="font-size:.76rem;color:var(--muted)">${(j.created_at||'').slice(0,16).replace('T',' ')}</td>
        </tr>`;

      document.getElementById('active-jobs-list').innerHTML = active.length
        ? `<table><tr><th>ID</th><th>Titre</th><th>Statut</th><th>Clips</th><th>Date</th></tr>
           ${active.map(renderRow).join('')}</table>`
        : '<div class="empty">Aucun job en cours.</div>';

      document.getElementById('recent-jobs-list').innerHTML = recent.length
        ? `<table><tr><th>ID</th><th>Titre</th><th>Statut</th><th>Clips</th><th>Date</th></tr>
           ${recent.map(renderRow).join('')}</table>`
        : '<div class="empty">Aucun projet dans l\'historique.</div>';

    } catch(e) {
      document.getElementById('recent-jobs-list').innerHTML =
        `<div style="color:#fe6b85;">Erreur : ${e.message}</div>`;
    }
  }

  async function clearCache() {
    if (!confirm('Vider le cache vidéo ? Les vidéos YouTube seront re-téléchargées la prochaine fois.')) return;
    const btn = document.getElementById('clear-cache-btn');
    btn.textContent = 'Suppression…';
    btn.disabled = true;
    try {
      const res  = await fetch('/api/admin/cache', { method: 'DELETE' });
      const data = await res.json();
      alert(`${data.deleted} fichier(s) supprimé(s).`);
      loadStats();
    } catch(e) {
      alert('Erreur : ' + e.message);
    }
    btn.textContent = '🗑️ Vider le cache';
    btn.disabled = false;
  }

  async function clearWhisperCache() {
    if (!confirm('Libérer le modèle Whisper de la RAM ? Il sera rechargé au prochain job.')) return;
    const btn = document.getElementById('clear-whisper-btn');
    btn.textContent = 'Libération…';
    btn.disabled = true;
    try {
      const res  = await fetch('/api/admin/whisper-cache', { method: 'DELETE' });
      const data = await res.json();
      const evicted = data.evicted.join(', ') || 'aucun';
      alert(`RAM libérée : modèle(s) «${evicted}» déchargé(s).`);
      loadWhisperCache();
    } catch(e) {
      alert('Erreur : ' + e.message);
    }
    btn.textContent = '🗑️ Libérer la RAM';
    btn.disabled = false;
  }

  async function loadTranscriptCache() {
    try {
      const res  = await fetch('/api/admin/transcript-cache');
      const data = await res.json();
      const el   = document.getElementById('transcript-cache-info');
      if (data.count === 0) {
        el.innerHTML = '<div class="empty">Aucune transcription en cache.</div>';
      } else {
        el.innerHTML = `
          <p style="font-size:.82rem;color:var(--muted);margin-bottom:10px;">
            ${data.count} transcription(s) — ${data.total_kb} KB total.
            Ces fichiers permettent de sauter Whisper sur les vidéos déjà traitées.
          </p>
          <table>
            <tr><th>Clé</th><th>Taille</th></tr>
            ${data.entries.slice(0, 20).map(e =>
              `<tr>
                <td style="font-family:monospace;font-size:.76rem;">${e.key}</td>
                <td>${e.size_kb} KB</td>
              </tr>`
            ).join('')}
          </table>`;
      }
    } catch(e) {
      document.getElementById('transcript-cache-info').innerHTML =
        `<div style="color:#fe6b85;">Erreur : ${e.message}</div>`;
    }
  }

  async function clearTranscriptCache() {
    if (!confirm('Supprimer toutes les transcriptions en cache ? Whisper devra re-transcrire les vidéos.')) return;
    const btn = document.getElementById('clear-transcript-btn');
    btn.textContent = 'Suppression…';
    btn.disabled = true;
    try {
      const res  = await fetch('/api/admin/transcript-cache', { method: 'DELETE' });
      const data = await res.json();
      alert(`${data.deleted} transcription(s) supprimée(s).`);
      loadTranscriptCache();
    } catch(e) {
      alert('Erreur : ' + e.message);
    }
    btn.textContent = '🗑️ Vider les transcriptions';
    btn.disabled = false;
  }

  async function loadCleanupStats() {
    try {
      const res  = await fetch('/api/cleanup/stats?max_age_hours=6');
      const data = await res.json();
      const el   = document.getElementById('cleanup-info');
      const d    = data.current_disk;

      el.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Vidéos téléchargées</div>
            <div class="stat-value" style="font-size:1.1rem;">${d.videos_total_mb} MB</div>
          </div>
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Transcriptions</div>
            <div class="stat-value" style="font-size:1.1rem;">${d.transcripts_total_mb} MB</div>
          </div>
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Uploads temp</div>
            <div class="stat-value" style="font-size:1.1rem;">${d.uploads_total_mb} MB</div>
          </div>
          <div class="stat-card" style="padding:10px 14px;">
            <div class="stat-label">Clips générés</div>
            <div class="stat-value" style="font-size:1.1rem;">${d.outputs_total_mb} MB</div>
          </div>
        </div>
        <div style="padding:10px 14px;border-radius:8px;border:1px solid var(--border);
                    font-size:.83rem;color:var(--muted);line-height:1.6;">
          <span style="color:var(--accent2);font-weight:700;">⚙️ Mode auto :</span>
          Suppression des fichiers de +6h toutes les 6h<br>
          <span style="color:var(--accent2);font-weight:700;">🗑️ Si lancé maintenant :</span>
          <strong style="color:var(--text);">${data.would_delete_count} élément(s)</strong>
          libérant <strong style="color:var(--text);">${data.would_free}</strong>
        </div>`;
    } catch(e) {
      document.getElementById('cleanup-info').innerHTML =
        `<div style="color:#fe6b85;">Erreur : ${e.message}</div>`;
    }
  }

  async function triggerCleanup() {
    const btn    = document.getElementById('cleanup-btn');
    const result = document.getElementById('cleanup-result');
    btn.textContent = 'Nettoyage…';
    btn.disabled    = true;
    result.style.display = 'none';
    try {
      const res  = await fetch('/api/cleanup?max_age_hours=6', { method: 'POST' });
      const data = await res.json();
      result.style.display = 'block';
      result.innerHTML = data.deleted_items === 0
        ? '✅ Cache déjà propre — aucun fichier de plus de 6h trouvé.'
        : `✅ Nettoyage terminé : <strong>${data.deleted_items} élément(s)</strong> supprimé(s),
           <strong>${data.freed}</strong> libérés.` +
          (data.errors.length
            ? `<br><span style="color:#fe6b85;">⚠️ ${data.errors.length} erreur(s) : ${data.errors[0]}</span>`
            : '');
      loadCleanupStats();
      loadStats();
    } catch(e) {
      result.style.display = 'block';
      result.style.borderColor = 'rgba(254,107,133,.4)';
      result.style.color       = '#fe6b85';
      result.innerHTML = '❌ Erreur : ' + e.message;
    }
    btn.textContent = '▶ Lancer maintenant';
    btn.disabled    = false;
  }

  // Auto-load on page open + auto-refresh every 10s
  loadStats();
  loadCleanupStats();
  setInterval(loadStats, 10000);
  setInterval(loadCleanupStats, 30000);
