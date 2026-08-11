const $ = id => document.getElementById(id);
const n = value => Number.isInteger(value) && value >= 0 ? value : 0;
const formatDate = value => value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : 'Time unavailable';

let photos = [];
let cameras = [];
let pipeline = {};
let mapboxToken = '';
let cameraMap = null;
let activeView = 'overview';

function photoLabels(item) {
  return Array.isArray(item.labels) ? item.labels.filter(label => label && typeof label === 'object') : [];
}

function photoAnimals(item) {
  return Array.isArray(item.animals) ? item.animals.filter(animal => animal && typeof animal === 'object') : [];
}

function needsReview(item) {
  const gate1 = item && item.gate1;
  const decision = item && item.review_decision;
  return Boolean(gate1 && gate1.route === 'review' && item.review_token && (!decision || decision.action === 'defer'));
}

async function submitReview(item, action, card) {
  const buttons = card.querySelectorAll('button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await fetch('/api/review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: item.review_token, action})
    });
    const data = await response.json().catch(() => ({ok: false}));
    if (!response.ok || !data.ok) throw new Error('The review decision could not be saved.');
    await fetchLibrary();
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });
    showError(error.message || 'The review decision could not be saved.');
  }
}

function setHealth(value) {
  const known = ['healthy', 'degraded', 'error'];
  const health = known.includes(value) ? value : 'unknown';
  $('health').textContent = health === 'healthy' ? 'Healthy' : health === 'unknown' ? 'Waiting' : health[0].toUpperCase() + health.slice(1);
  $('nav-status').textContent = health === 'healthy' ? 'Archive healthy' : health === 'unknown' ? 'No completed runs' : 'Attention required';
  $('nav-dot').className = 'pulse ' + health;
  $('health-detail').textContent = health === 'healthy' ? 'Latest run completed' : health === 'unknown' ? 'Waiting for first run' : 'Review recent runs';
}

function renderRuns(runs) {
  const body = $('runs');
  body.replaceChildren();
  if (!runs.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.textContent = 'No synchronization runs recorded yet.';
    return;
  }
  runs.forEach(run => {
    const row = body.insertRow();
    const verified = run.verified || {};
    const verifiedUnits = Math.min(n(verified.image), n(verified.metadata), n(verified.checksum));
    [formatDate(run.finished_at), run.status || 'unknown', n(run.downloaded), verifiedUnits, n(run.failed)].forEach((value, index) => {
      const cell = row.insertCell();
      if (index === 1) {
        const pill = document.createElement('span');
        pill.className = 'status-pill ' + value;
        pill.textContent = value;
        cell.appendChild(pill);
      } else {
        cell.textContent = value;
      }
    });
  });
}

function makeChip(text, className = '') {
  const chip = document.createElement('span');
  chip.className = 'chip' + (className ? ' ' + className : '');
  chip.textContent = text;
  return chip;
}

function makePhotoCard(item, options = {}) {
  const card = document.createElement('article');
  card.className = 'card photo-card' + (options.review ? ' review-card' : '');
  const image = document.createElement('img');
  image.src = item.preview_url;
  image.alt = 'Archived Reveal camera capture';
  image.loading = 'lazy';
  image.referrerPolicy = 'no-referrer';
  const meta = document.createElement('div');
  meta.className = 'photo-meta';
  const title = document.createElement('strong');
  title.textContent = item.camera_name || 'Reveal camera';
  const captured = document.createElement('div');
  captured.className = 'photo-date';
  captured.textContent = formatDate(item.captured_at);
  const chips = document.createElement('div');
  chips.className = 'chips';
  photoLabels(item).slice(0, 3).forEach(label => chips.appendChild(makeChip(String(label.label || 'label'))));
  photoAnimals(item).slice(0, 2).forEach(animal => chips.appendChild(makeChip(String(animal.name || animal.profile_name || 'Named deer'))));
  if (needsReview(item)) chips.appendChild(makeChip('Needs review', 'review'));
  if (item.variant) chips.appendChild(makeChip(item.variant === 'cloud_thumbnail' ? 'Cloud thumbnail' : String(item.variant).replaceAll('_', ' ')));
  meta.append(title, captured, chips);
  if (options.review && item.gate1) {
    const evidence = document.createElement('div');
    evidence.className = 'gate1-evidence';
    const species = item.gate1.species_label || 'Uncertain animal';
    const confidence = Math.round(100 * Number(item.gate1.species_confidence || item.gate1.animal_confidence || 0));
    evidence.textContent = `${species} · ${confidence}% · ${String(item.gate1.reason || 'model selected').replaceAll('_', ' ')}`;
    const actions = document.createElement('div');
    actions.className = 'review-actions';
    [
      ['request_hd', 'Request HD'],
      ['keep_for_identity', 'Keep for ID'],
      ['not_useful', 'Not useful'],
      ['defer', 'Defer']
    ].forEach(([action, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.reviewAction = action;
      button.textContent = label;
      button.addEventListener('click', event => {
        event.stopPropagation();
        submitReview(item, action, card);
      });
      actions.appendChild(button);
    });
    meta.append(evidence, actions);
  }
  card.append(image, meta);
  return card;
}

function renderPhotoGrid(target, items, options = {}) {
  target.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'card empty';
    empty.style.gridColumn = '1/-1';
    const inner = document.createElement('div');
    inner.className = 'empty-inner';
    const strong = document.createElement('strong');
    strong.textContent = options.emptyTitle || 'No photos here yet';
    const copy = document.createElement('span');
    copy.textContent = options.emptyCopy || 'The next catalog-enabled sync will populate this view.';
    inner.append(strong, copy);
    empty.appendChild(inner);
    target.appendChild(empty);
    return;
  }
  items.forEach(item => target.appendChild(makePhotoCard(item, options)));
}

function populateCameraFilter() {
  const select = $('camera-filter');
  while (select.options.length > 1) select.remove(1);
  const names = [...new Set(photos.map(item => item.camera_name).filter(Boolean))].sort();
  names.forEach(name => {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  });
}

function renderFilteredPhotos() {
  const camera = $('camera-filter').value;
  const labelState = $('label-filter').value;
  const filtered = photos.filter(item => {
    if (camera && item.camera_name !== camera) return false;
    if (labelState === 'review' && !needsReview(item)) return false;
    if (labelState === 'labeled' && needsReview(item)) return false;
    return true;
  });
  const total = n(pipeline.total_thumbnails) || photos.length;
  $('photo-summary').textContent = `${filtered.length} of ${photos.length} recent captures shown · ${total} total cataloged`;
  renderPhotoGrid($('library-view'), filtered);
}

function renderReview() {
  const queue = photos.filter(needsReview);
  const unresolved = n(pipeline.unresolved_review) || queue.length;
  $('review-summary').textContent = queue.length < unresolved
    ? `Showing ${queue.length} of ${unresolved} awaiting a human decision`
    : `${unresolved} awaiting a human decision`;
  renderPhotoGrid($('review-grid'), queue, {
    review: true,
    emptyTitle: 'Review queue is clear',
    emptyCopy: 'Gate 1 has no unresolved model-selected photos.'
  });
}

function renderPipeline() {
  const values = {
    'pipeline-total': pipeline.total_thumbnails,
    'pipeline-assessed': pipeline.assessed_thumbnails,
    'pipeline-review': pipeline.review_representatives,
    'pipeline-duplicates': pipeline.event_duplicates,
    'pipeline-blanks': pipeline.blank_or_below_threshold,
    'pipeline-nontarget': pipeline.confident_non_target,
    'pipeline-pending': pipeline.pending_thumbnails
  };
  Object.entries(values).forEach(([id, value]) => { $(id).textContent = n(value); });
  const model = pipeline.model_name || 'Model';
  const version = pipeline.model_version || 'unknown version';
  $('pipeline-model').textContent = `${model} ${version} · one representative per five-second camera event`;
}

function collectProfiles() {
  const profiles = new Map();
  photos.forEach(item => photoAnimals(item).forEach(animal => {
    const key = String(animal.id || animal.profile_id || animal.name || animal.profile_name || '');
    if (!key) return;
    const existing = profiles.get(key) || {name: animal.name || animal.profile_name || 'Named deer', photos: []};
    existing.photos.push(item);
    profiles.set(key, existing);
  }));
  return [...profiles.values()];
}

function renderDeerProfiles() {
  const profiles = collectProfiles();
  const target = $('deer-grid');
  target.replaceChildren();
  $('deer-summary').textContent = `${profiles.length} human-confirmed profiles`;
  if (!profiles.length) {
    const empty = document.createElement('div');
    empty.className = 'card empty';
    empty.style.gridColumn = '1/-1';
    const inner = document.createElement('div');
    inner.className = 'empty-inner';
    const title = document.createElement('strong');
    title.textContent = 'No deer profiles yet';
    const copy = document.createElement('span');
    copy.textContent = 'Confirmed identities will appear here with season-scoped photo history. Suggestions will remain separate until a person confirms them.';
    inner.append(title, copy);
    empty.appendChild(inner);
    target.appendChild(empty);
    return;
  }
  profiles.forEach(profile => {
    const item = profile.photos[0];
    const card = document.createElement('article');
    card.className = 'card deer-card';
    const image = document.createElement('img');
    image.src = item.preview_url;
    image.alt = `Profile photo for ${profile.name}`;
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';
    const meta = document.createElement('div');
    meta.className = 'photo-meta';
    const title = document.createElement('strong');
    title.textContent = profile.name;
    const copy = document.createElement('div');
    copy.className = 'photo-date';
    copy.textContent = `${profile.photos.length} confirmed ${profile.photos.length === 1 ? 'photo' : 'photos'}`;
    meta.append(title, copy);
    card.append(image, meta);
    target.appendChild(card);
  });
}

function renderCameraCards() {
  const list = $('camera-list');
  list.replaceChildren();
  cameras.forEach(camera => {
    const card = document.createElement('article');
    card.className = 'card camera-card';
    const name = document.createElement('strong');
    name.textContent = camera.name || 'Reveal camera';
    const place = document.createElement('span');
    place.textContent = camera.location_name || 'Location not named';
    const health = document.createElement('span');
    health.textContent = `Battery ${camera.battery_level ?? '—'} · Signal ${camera.signal_level ?? '—'}`;
    const seen = document.createElement('span');
    seen.textContent = camera.last_seen_at ? `Last seen ${formatDate(camera.last_seen_at)}` : 'Last-seen time unavailable';
    card.append(name, place, health, seen);
    list.appendChild(card);
  });
}

function mapPlaceholder(message) {
  const target = $('camera-map');
  target.replaceChildren();
  const placeholder = document.createElement('div');
  placeholder.className = 'map-placeholder';
  const copy = document.createElement('div');
  const heading = document.createElement('strong');
  heading.textContent = 'Satellite map unavailable';
  copy.append(heading, document.createElement('br'), message);
  placeholder.appendChild(copy);
  target.appendChild(placeholder);
}

function loadCameraMap() {
  const located = cameras.filter(camera => Number.isFinite(Number(camera.latitude)) && Number.isFinite(Number(camera.longitude)));
  if (cameraMap) {
    setTimeout(() => cameraMap.invalidateSize(), 0);
    return;
  }
  if (!located.length) return mapPlaceholder('No provider GPS positions are cataloged yet.');
  if (!window.L) return mapPlaceholder('The local map library did not load.');
  const target = $('camera-map');
  target.replaceChildren();
  cameraMap = L.map(target, {zoomControl: true, attributionControl: true});
  const hasMapbox = /^pk\.[A-Za-z0-9._-]{8,500}$/.test(mapboxToken);
  const tileUrl = hasMapbox
    ? 'https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/512/{z}/{x}/{y}@2x?access_token={accessToken}'
    : 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
  const tileOptions = hasMapbox
    ? {accessToken: mapboxToken, tileSize: 512, zoomOffset: -1, maxZoom: 22, attribution: '© Mapbox © OpenStreetMap'}
    : {maxZoom: 19, attribution: 'Tiles © Esri'};
  L.tileLayer(tileUrl, tileOptions).addTo(cameraMap);
  const bounds = [];
  located.forEach(camera => {
    const point = [Number(camera.latitude), Number(camera.longitude)];
    bounds.push(point);
    const marker = L.circleMarker(point, {radius: 9, weight: 3, color: '#f4f2ec', fillColor: '#82b889', fillOpacity: .95}).addTo(cameraMap);
    const popup = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = camera.name || 'Reveal camera';
    const details = document.createElement('div');
    details.textContent = `${camera.location_name || 'Location not named'} · Battery ${camera.battery_level ?? '—'} · Signal ${camera.signal_level ?? '—'}`;
    popup.append(name, details);
    marker.bindPopup(popup);
  });
  cameraMap.fitBounds(bounds, {padding: [32, 32], maxZoom: 17});
}

function showView(name) {
  const allowed = ['overview', 'review', 'deer', 'cameras', 'photos'];
  if (!allowed.includes(name)) name = 'overview';
  activeView = name;
  document.querySelectorAll('[data-view-panel]').forEach(panel => { panel.hidden = panel.dataset.viewPanel !== name; });
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  history.replaceState(null, '', name === 'overview' ? location.pathname : `#${name}`);
  if (name === 'cameras') setTimeout(loadCameraMap, 0);
  window.scrollTo({top: 0, behavior: 'instant'});
}

function updateCatalogViews() {
  const review = photos.filter(needsReview);
  const profiles = collectProfiles();
  const total = n(pipeline.total_thumbnails) || photos.length;
  const unresolved = n(pipeline.unresolved_review) || review.length;
  $('catalog-count').textContent = total;
  $('review-count').textContent = unresolved;
  $('camera-count').textContent = cameras.length;
  $('review-nav-count').textContent = unresolved;
  $('deer-nav-count').textContent = profiles.length;
  $('camera-nav-count').textContent = cameras.length;
  $('photo-nav-count').textContent = total;
  renderPipeline();
  renderPhotoGrid($('recent-photos'), photos.slice(0, 8), {emptyTitle: 'No cataloged photos yet'});
  populateCameraFilter();
  renderFilteredPhotos();
  renderReview();
  renderDeerProfiles();
  renderCameraCards();
  if (activeView === 'cameras') loadCameraMap();
}

async function fetchLibrary() {
  const response = await fetch('/api/library', {cache: 'no-store'});
  const data = await response.json().catch(() => ({ok: false}));
  if (!response.ok || !data.ok) throw new Error('The photo catalog is temporarily unavailable.');
  photos = Array.isArray(data.photos) ? data.photos : [];
  cameras = Array.isArray(data.cameras) ? data.cameras : [];
  pipeline = data.pipeline && typeof data.pipeline === 'object' ? data.pipeline : {};
  mapboxToken = typeof data.mapbox_access_token === 'string' ? data.mapbox_access_token : '';
  updateCatalogViews();
}

async function refreshStatus() {
  const response = await fetch('/api/status', {cache: 'no-store'});
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error('Archive status is temporarily unavailable.');
  setHealth(data.health);
  $('updated').textContent = `Last archive update · ${formatDate(data.updated_at)}`;
  const latest = data.latest || {};
  const verified = latest.verified || {};
  [['images', 'image', 'image-check'], ['metadata', 'metadata', 'metadata-check'], ['checksums', 'checksum', 'checksum-check']].forEach(([valueId, field, checkId]) => {
    const count = n(verified[field]);
    $(valueId).textContent = count;
    $(checkId).className = 'check' + (count > 0 ? ' verified' : '');
    $(checkId).textContent = count > 0 ? '✓' : '·';
  });
  renderRuns(Array.isArray(data.recent_runs) ? data.recent_runs : []);
}

function showError(message) {
  $('error').textContent = message;
  $('error').style.display = 'block';
}

async function initialize() {
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  document.querySelectorAll('[data-open-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.openView)));
  $('camera-filter').addEventListener('change', renderFilteredPhotos);
  $('label-filter').addEventListener('change', renderFilteredPhotos);
  const requestedView = location.hash.slice(1);
  showView(requestedView || 'overview');
  const results = await Promise.allSettled([refreshStatus(), fetchLibrary()]);
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) showError(failures.map(result => result.reason.message).join(' '));
  $('loading-line').classList.add('done');
}

initialize();
setInterval(() => refreshStatus().catch(() => {}), 30000);
