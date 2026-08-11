const $ = id => document.getElementById(id);
const n = value => Number.isInteger(value) && value >= 0 ? value : 0;
const date = value => value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : 'Not available';

function setHealth(value) {
  const known = ['healthy', 'degraded', 'error'];
  const health = known.includes(value) ? value : 'unknown';
  $('health').textContent = health[0].toUpperCase() + health.slice(1);
  $('nav-status').textContent = health === 'healthy' ? 'All systems reporting' : health === 'unknown' ? 'No completed runs' : 'Attention required';
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
    cell.className = 'empty';
    cell.textContent = 'No synchronization runs have been recorded yet.';
    return;
  }
  runs.forEach(run => {
    const row = body.insertRow();
    const verified = run.verified || {};
    const verifiedUnits = Math.min(n(verified.image), n(verified.metadata), n(verified.checksum));
    const values = [date(run.finished_at), run.status || 'unknown', n(run.downloaded), verifiedUnits, n(run.failed)];
    values.forEach((value, index) => {
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

let cameraMap = null;
let recoveryToken = null;

function renderLibraryPhotos(photos) {
  const view = $('library-view');
  view.replaceChildren();
  view.className = 'grid library-photos';
  if (!photos.length) {
    const empty = document.createElement('div');
    empty.className = 'card empty';
    empty.style.gridColumn = '1/-1';
    empty.textContent = 'No cataloged photos yet. Run a catalog-enabled synchronization to populate the library.';
    view.appendChild(empty);
    return;
  }
  photos.forEach(item => {
    const card = document.createElement('article');
    card.className = 'card library-photo';
    const image = document.createElement('img');
    image.src = item.preview_url;
    image.alt = 'Private Reveal archive photo';
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';
    const meta = document.createElement('div');
    meta.className = 'library-photo-meta';
    const title = document.createElement('strong');
    title.textContent = item.camera_name || 'Reveal camera';
    const captured = document.createElement('div');
    captured.className = 'section-copy';
    captured.textContent = date(item.captured_at);
    meta.append(title, captured);
    const chips = document.createElement('div');
    chips.className = 'chips';
    (Array.isArray(item.labels) ? item.labels : []).slice(0, 4).forEach(label => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = String(label.label || 'label');
      chips.appendChild(chip);
    });
    if (item.variant) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = item.variant === 'cloud_thumbnail' ? 'Cloud thumbnail' : item.variant;
      chips.appendChild(chip);
    }
    meta.appendChild(chips);
    card.append(image, meta);
    view.appendChild(card);
  });
}

function renderCameraCards(cameras) {
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
    health.textContent = 'Battery ' + (camera.battery_level ?? '—') + ' · Signal ' + (camera.signal_level ?? '—');
    card.append(name, place, health);
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
  copy.appendChild(heading);
  copy.appendChild(document.createElement('br'));
  copy.append(message);
  placeholder.appendChild(copy);
  target.appendChild(placeholder);
}

function loadMapboxMap(token, cameras) {
  const located = cameras.filter(camera => Number.isFinite(Number(camera.latitude)) && Number.isFinite(Number(camera.longitude)));
  if (cameraMap) {
    cameraMap.remove();
    cameraMap = null;
  }
  if (!/^pk\.[A-Za-z0-9._-]{8,500}$/.test(token || '')) {
    mapPlaceholder('The production Mapbox browser token is not configured.');
    return;
  }
  if (!located.length) {
    mapPlaceholder('No provider GPS positions are cataloged yet.');
    return;
  }
  if (!window.L) {
    mapPlaceholder('The local map library did not load.');
    return;
  }
  const target = $('camera-map');
  target.replaceChildren();
  cameraMap = L.map(target, {zoomControl: true, attributionControl: true});
  L.tileLayer('https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/512/{z}/{x}/{y}@2x?access_token={accessToken}', {
    accessToken: token,
    tileSize: 512,
    zoomOffset: -1,
    maxZoom: 22,
    attribution: '© Mapbox © OpenStreetMap'
  }).addTo(cameraMap);
  const bounds = [];
  located.forEach(camera => {
    const point = [Number(camera.latitude), Number(camera.longitude)];
    bounds.push(point);
    const marker = L.circleMarker(point, {
      radius: 9,
      weight: 3,
      color: '#f3f5f3',
      fillColor: '#3ecf8e',
      fillOpacity: 0.9
    }).addTo(cameraMap);
    const popup = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = camera.name || 'Reveal camera';
    const details = document.createElement('div');
    details.textContent = (camera.location_name || 'Location not named') + ' · Battery ' + (camera.battery_level ?? '—') + ' · Signal ' + (camera.signal_level ?? '—');
    popup.append(name, details);
    marker.bindPopup(popup);
  });
  cameraMap.fitBounds(bounds, {padding: [28, 28], maxZoom: 17});
}

function showPrivateView(kind) {
  const mapVisible = kind === 'map';
  $('map-view').hidden = !mapVisible;
  $('library-view').hidden = mapVisible;
  $('show-map').classList.toggle('active', mapVisible);
  $('show-library').classList.toggle('active', !mapVisible);
  if (mapVisible && cameraMap) {
    setTimeout(() => cameraMap.invalidateSize(), 0);
  }
}

async function authAction(payload) {
  const response = await fetch('/api/auth', {
    method: 'POST',
    cache: 'no-store',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const data = await response.json().catch(() => ({ok: false, error: 'Authentication service unavailable'}));
  if (!response.ok || !data.ok) {
    throw new Error(data.error || 'Authentication failed');
  }
  return data;
}

async function fetchPrivateLibrary() {
  const response = await fetch('/api/library', {cache: 'no-store'});
  const data = await response.json().catch(() => ({ok: false}));
  if (!response.ok || !data.ok) {
    throw new Error(response.status === 401 ? 'Sign in to open the private library.' : 'Private catalog is not active yet.');
  }
  $('library-lock').hidden = true;
  $('library-shell').hidden = false;
  renderLibraryPhotos(Array.isArray(data.photos) ? data.photos : []);
  const cameras = Array.isArray(data.cameras) ? data.cameras : [];
  renderCameraCards(cameras);
  loadMapboxMap(data.mapbox_access_token, cameras);
}

function showSignedOut(message) {
  $('library-shell').hidden = true;
  $('library-lock').hidden = false;
  $('library-auth').hidden = false;
  $('password-setup').hidden = true;
  $('library-password').value = '';
  $('library-message').textContent = message || 'Sign in to view private photos and exact camera locations.';
  if (cameraMap) {
    cameraMap.remove();
    cameraMap = null;
  }
}

$('library-auth').addEventListener('submit', async event => {
  event.preventDefault();
  const email = $('library-email').value.trim();
  const password = $('library-password').value;
  const button = event.submitter;
  if (button) button.disabled = true;
  $('library-message').textContent = 'Signing in…';
  try {
    await authAction({action: 'login', email, password});
    $('library-password').value = '';
    await fetchPrivateLibrary();
    $('library-message').textContent = '';
  } catch (error) {
    showSignedOut(error.message);
  } finally {
    if (button) button.disabled = false;
  }
});

$('recover-password').addEventListener('click', async () => {
  const email = $('library-email').value.trim();
  $('library-message').textContent = 'Requesting password email…';
  try {
    const data = await authAction({action: 'recover', email});
    $('library-message').textContent = data.message;
  } catch (error) {
    $('library-message').textContent = error.message;
  }
});

$('password-setup').addEventListener('submit', async event => {
  event.preventDefault();
  const password = $('new-password').value;
  $('library-message').textContent = 'Setting password…';
  try {
    await authAction({action: 'update_password', access_token: recoveryToken, password});
    recoveryToken = null;
    $('new-password').value = '';
    showSignedOut('Password saved. Sign in with your email and new password.');
  } catch (error) {
    $('library-message').textContent = error.message;
  }
});

$('show-library').addEventListener('click', () => showPrivateView('library'));
$('show-map').addEventListener('click', () => showPrivateView('map'));
$('lock-library').addEventListener('click', async () => {
  await authAction({action: 'logout'}).catch(() => {});
  showSignedOut('Signed out.');
});

function consumePasswordSetupLink() {
  if (!location.hash) return false;
  const values = new URLSearchParams(location.hash.slice(1));
  const type = values.get('type');
  const token = values.get('access_token');
  history.replaceState(null, '', location.pathname + location.search);
  if ((type === 'recovery' || type === 'invite') && /^[A-Za-z0-9._~-]{10,8192}$/.test(token || '')) {
    recoveryToken = token;
    $('library-auth').hidden = true;
    $('password-setup').hidden = false;
    $('library-lock').hidden = false;
    $('library-message').textContent = type === 'invite' ? 'Choose a password to finish creating your DeerID account.' : 'Choose a new password.';
    return true;
  }
  return false;
}

async function restoreSession() {
  if (consumePasswordSetupLink()) return;
  const response = await fetch('/api/auth', {cache: 'no-store'}).catch(() => null);
  if (!response || !response.ok) {
    showSignedOut();
    return;
  }
  try {
    await fetchPrivateLibrary();
  } catch (error) {
    showSignedOut(error.message);
  }
}

async function refresh() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error('Status service unavailable');
    $('error').style.display = 'none';
    setHealth(data.health);
    $('updated').textContent = 'Last durable update · ' + date(data.updated_at);
    const latest = data.latest || {};
    $('downloaded').textContent = n(latest.downloaded);
    $('skipped').textContent = n(latest.skipped);
    $('failed').textContent = n(latest.failed);
    const verified = latest.verified || {};
    $('images').textContent = n(verified.image);
    $('metadata').textContent = n(verified.metadata);
    $('checksums').textContent = n(verified.checksum);
    [['image-check', verified.image], ['metadata-check', verified.metadata], ['checksum-check', verified.checksum]].forEach(([id, count]) => {
      const mark = $(id);
      const complete = n(count) > 0;
      mark.className = 'check' + (complete ? ' verified' : '');
      mark.textContent = complete ? '✓' : '·';
    });
    renderRuns(Array.isArray(data.recent_runs) ? data.recent_runs : []);
  } catch (_) {
    setHealth('error');
    $('updated').textContent = 'Durable status is temporarily unavailable';
    $('error').textContent = 'The dashboard could not read its private status manifest. Synchronization remains fail-closed.';
    $('error').style.display = 'block';
  }
}

restoreSession();
refresh();
setInterval(refresh, 30000);
