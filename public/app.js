const $ = id => document.getElementById(id);
const n = value => Number.isInteger(value) && value >= 0 ? value : 0;
const formatDate = value => value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : 'Time unavailable';

let photos = [];
let cameras = [];
let deerProfiles = [];
let pipeline = {};
let gate1bMetrics = {};
let operationalStats = {};
let automationAudit = [];
let hdReviewQueue = [];
let activeReviewQueue = 'uncertain';
let mapboxToken = '';
let cameraMap = null;
let activeView = 'overview';
let reviewQueue = [];
let reviewRefreshInFlight = false;
const pendingReviewIds = new Set();
const decidedReviewIds = new Set();
const deferredReviewIds = new Set();

function photoLabels(item) {
  return Array.isArray(item.labels) ? item.labels.filter(label => label && typeof label === 'object') : [];
}

function photoAnimals(item) {
  return Array.isArray(item.animals) ? item.animals.filter(animal => animal && typeof animal === 'object') : [];
}

function needsReview(item) {
  const gate1 = item && item.gate1;
  const gate1b = item && item.gate1b;
  const decision = item && item.review_decision;
  return Boolean(
    gate1 && gate1.route === 'review' && item.review_token
    && (!gate1b || gate1b.queue !== 'suppressed')
    && (!decision || decision.action === 'defer')
  );
}

function reviewQueueName(item) {
  const gate1b = item && item.gate1b;
  return gate1b && gate1b.queue === 'uncertain' ? 'uncertain' : 'automated';
}

function belongsToActiveQueue(item) {
  return needsReview(item) && reviewQueueName(item) === activeReviewQueue;
}

async function submitGate1bLabel(item, fields, button) {
  if (!item.review_token || button.disabled) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Saving…';
  try {
    const payload = {token: item.review_token};
    Object.entries(fields).forEach(([name, select]) => { payload[name] = select.value; });
    const response = await fetch('/api/gate1b_label', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({ok: false}));
    if (!response.ok || !data.ok) throw new Error('Corrections could not be saved.');
    item.gate1b = item.gate1b || {queue: 'uncertain'};
    item.gate1b.human_label = {
      species_label: fields.species_label.value,
      visible_antler: fields.visible_antler.value,
      probable_male: fields.probable_male.value,
      head_visibility: fields.head_visibility.value
    };
    button.textContent = 'Corrections saved';
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showError(error.message || 'Corrections could not be saved.');
  }
}

async function submitReview(item, action, card) {
  if (pendingReviewIds.has(item.id)) return;
  pendingReviewIds.add(item.id);
  reviewQueue = reviewQueue.filter(candidate => candidate.id !== item.id);
  const resolves = action !== 'defer';
  if (resolves) pipeline.unresolved_review = Math.max(0, n(pipeline.unresolved_review) - 1);
  card.classList.add('review-exit-left');
  setTimeout(() => renderReview(true), 120);
  preloadReviewQueue(5);
  try {
    const response = await fetch('/api/review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: item.review_token, action})
    });
    const data = await response.json().catch(() => ({ok: false}));
    if (!response.ok || !data.ok) throw new Error(
      action === 'request_hd' ? 'Reveal did not accept the HD request. The photo was returned to the queue.' : 'The review decision could not be saved.'
    );
    pendingReviewIds.delete(item.id);
    if (resolves) decidedReviewIds.add(item.id);
    else deferredReviewIds.add(item.id);
    updateReviewCounts();
    // Refill only after all concurrent decisions have committed so an older
    // server snapshot cannot overwrite the optimistic unresolved count.
    if (reviewQueue.length < 10 && pendingReviewIds.size === 0) refreshReviewBuffer();
  } catch (error) {
    pendingReviewIds.delete(item.id);
    reviewQueue.push(item);
    if (resolves) pipeline.unresolved_review = n(pipeline.unresolved_review) + 1;
    showError(error.message || 'The review decision could not be saved.');
    updateReviewCounts();
    if (!reviewQueue.some(candidate => candidate.id !== item.id)) renderReview(true);
    if (reviewQueue.length < 10 && pendingReviewIds.size === 0) refreshReviewBuffer();
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

function makeCorrectionControls(item) {
  const prediction = item.gate1b || {};
  const current = prediction.human_label || prediction;
  const form = document.createElement('div');
  form.className = 'gate1b-corrections';
  const fields = {};
  const definitions = [
    ['species_label', 'Species', [
      ['whitetail', 'Whitetail'], ['axis', 'Axis deer'], ['other_deer', 'Other deer'],
      ['non_deer', 'Not deer'], ['unknown', 'Unknown']
    ]],
    ['visible_antler', 'Antlers', [['yes', 'Visible'], ['no', 'Not visible'], ['unknown', 'Unknown']]],
    ['probable_male', 'Male', [['yes', 'Probable male'], ['no', 'No male evidence'], ['unknown', 'Unknown']]],
    ['head_visibility', 'Head', [['full', 'Fully visible'], ['partial', 'Partly visible'], ['none', 'Not visible'], ['unknown', 'Unknown']]]
  ];
  definitions.forEach(([name, labelText, choices]) => {
    const label = document.createElement('label');
    const caption = document.createElement('span');
    caption.textContent = labelText;
    const select = document.createElement('select');
    select.name = name;
    choices.forEach(([value, text]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = text;
      select.appendChild(option);
    });
    select.value = choices.some(([value]) => value === current[name]) ? current[name] : 'unknown';
    fields[name] = select;
    label.append(caption, select);
    form.appendChild(label);
  });
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'save-corrections';
  save.textContent = 'Save corrections';
  save.addEventListener('click', event => {
    event.stopPropagation();
    submitGate1bLabel(item, fields, save);
  });
  form.appendChild(save);
  return form;
}

async function submitProfileAssignment(item, payload, button) {
  if (!item.review_token || button.disabled) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Saving…';
  try {
    const response = await fetch('/api/profile_assignment', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: item.review_token, ...payload})
    });
    const data = await response.json().catch(() => ({ok: false}));
    if (!response.ok || !data.ok) throw new Error('The deer profile could not be updated.');
    if (payload.action === 'create') {
      button.textContent = data.created === false ? 'Profile already exists' : 'Profile created';
    } else {
      button.textContent = 'Photo added';
    }
    await fetchLibrary({preserveReview: true});
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showError(error.message || 'The deer profile could not be updated.');
  }
}

function makeProfileControls(item) {
  const section = document.createElement('div');
  section.className = 'profile-assignment';
  const heading = document.createElement('strong');
  heading.textContent = 'Deer identity profile';
  const capturedYear = new Date(item.captured_at).getFullYear();
  const compatible = deerProfiles.filter(profile => Number(profile.season_year) === capturedYear);

  const existingRow = document.createElement('div');
  existingRow.className = 'profile-assignment-row';
  const select = document.createElement('select');
  select.setAttribute('aria-label', 'Existing deer profile');
  compatible.forEach(profile => {
    const option = document.createElement('option');
    option.value = profile.id;
    option.textContent = `${profile.display_name} · ${profile.season_year} · ${profile.photo_count} photos`;
    select.appendChild(option);
  });
  const attach = document.createElement('button');
  attach.type = 'button';
  attach.textContent = 'Add photo to profile';
  attach.disabled = !compatible.length;
  attach.addEventListener('click', event => {
    event.stopPropagation();
    submitProfileAssignment(item, {action: 'attach', profile_id: select.value}, attach);
  });
  existingRow.append(select, attach);

  const createRow = document.createElement('div');
  createRow.className = 'profile-create-row';
  const name = document.createElement('input');
  name.type = 'text';
  name.maxLength = 80;
  name.placeholder = 'New deer name';
  name.setAttribute('aria-label', 'New deer profile name');
  const species = document.createElement('select');
  [['white-tailed deer', 'Whitetail'], ['axis deer', 'Axis deer'], ['other deer', 'Other deer']].forEach(([value, label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label; species.appendChild(option);
  });
  if (item.gate1b && item.gate1b.species_label === 'axis') species.value = 'axis deer';
  const sex = document.createElement('select');
  [['male', 'Male'], ['female', 'Female'], ['unknown', 'Unknown sex']].forEach(([value, label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label; sex.appendChild(option);
  });
  if (!(item.gate1b && item.gate1b.probable_male === 'yes')) sex.value = 'unknown';
  const create = document.createElement('button');
  create.type = 'button';
  create.textContent = 'Create new deer profile';
  create.addEventListener('click', event => {
    event.stopPropagation();
    submitProfileAssignment(item, {
      action: 'create', display_name: name.value, species: species.value, sex: sex.value
    }, create);
  });
  createRow.append(name, species, sex, create);
  section.append(heading, existingRow, createRow);
  return section;
}

function makePhotoCard(item, options = {}) {
  const card = document.createElement('article');
  card.className = 'card photo-card' + (options.review ? ' review-card' : '');
  const image = document.createElement('img');
  image.src = item.preview_url;
  image.alt = 'Archived Reveal camera capture';
  image.loading = options.review ? 'eager' : 'lazy';
  image.decoding = 'async';
  image.referrerPolicy = 'no-referrer';
  const meta = document.createElement('div');
  meta.className = 'photo-meta';
  let quickActions = null;
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
    const gate1b = item.gate1b || {};
    const modelBits = gate1b.prediction_id
      ? `${String(gate1b.species_label || 'unknown').replaceAll('_', ' ')} · antlers ${gate1b.visible_antler || 'unknown'} · male ${gate1b.probable_male || 'unknown'} · head ${gate1b.head_visibility || 'unknown'}`
      : 'Gate 1B has not assessed this event yet';
    evidence.textContent = `${species} · ${confidence}% · ${modelBits}`;
    const modelReason = document.createElement('div');
    modelReason.className = 'gate1b-reason';
    modelReason.textContent = gate1b.reason || String(item.gate1.reason || 'model selected').replaceAll('_', ' ');
    const corrections = makeCorrectionControls(item);
    const profileControls = makeProfileControls(item);
    quickActions = document.createElement('div');
    quickActions.className = 'review-actions';
    [
      ['request_hd', 'Pass → Request HD'],
      ['not_useful', 'Not useful'],
      ['defer', 'Defer']
    ].forEach(([action, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.reviewAction = action;
      if (action === 'request_hd' && gate1b.hd_recommended) button.classList.add('hd-priority');
      button.textContent = label;
      button.addEventListener('click', event => {
        event.stopPropagation();
        submitReview(item, action, card);
      });
      quickActions.appendChild(button);
    });
    meta.append(evidence, modelReason, corrections, profileControls);
  }
  if (quickActions) card.append(image, quickActions, meta);
  else card.append(image, meta);
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

function updateReviewCounts() {
  const unresolved = n(pipeline.unresolved_review) || photos.filter(needsReview).length;
  const activeCount = photos.filter(belongsToActiveQueue).length;
  $('review-count').textContent = unresolved;
  $('review-nav-count').textContent = unresolved;
  $('review-summary').textContent = activeCount
    ? `${activeCount} loaded in ${activeReviewQueue.replaceAll('_', ' ')} · ${Math.min(5, Math.max(0, reviewQueue.length - 1))} next photos ready`
    : `No loaded events in ${activeReviewQueue.replaceAll('_', ' ')}`;
  document.querySelectorAll('[data-review-queue]').forEach(button => {
    const queue = button.dataset.reviewQueue;
    button.classList.toggle('active', queue === activeReviewQueue);
    const count = photos.filter(item => needsReview(item) && reviewQueueName(item) === queue).length;
    const counter = button.querySelector('[data-queue-count]');
    if (counter) counter.textContent = count;
  });
}

function preloadReviewQueue(count) {
  reviewQueue.slice(1, count + 1).forEach(item => {
    const image = new Image();
    image.decoding = 'async';
    image.referrerPolicy = 'no-referrer';
    image.src = item.preview_url;
  });
}

function renderReview(animate = false) {
  const stage = $('review-stage');
  stage.replaceChildren();
  updateReviewCounts();
  const item = reviewQueue[0];
  if (!item) {
    const empty = document.createElement('div');
    empty.className = 'card empty';
    const inner = document.createElement('div');
    inner.className = 'empty-inner';
    const title = document.createElement('strong');
    title.textContent = 'Review queue is clear';
    const copy = document.createElement('span');
    copy.textContent = 'Gate 1 has no unresolved model-selected photos in the ready buffer.';
    inner.append(title, copy);
    empty.appendChild(inner);
    stage.appendChild(empty);
    return;
  }
  const card = makePhotoCard(item, {review: true});
  card.classList.add('review-focus-card');
  if (animate) card.classList.add('review-enter-right');
  stage.appendChild(card);
  preloadReviewQueue(5);
}

function mergeReviewQueue(incoming, preserveCurrent) {
  const available = incoming.filter(item =>
    !decidedReviewIds.has(item.id) && !pendingReviewIds.has(item.id) && !deferredReviewIds.has(item.id)
  );
  if (!preserveCurrent) {
    reviewQueue = available;
    return;
  }
  const fresh = new Map(available.map(item => [item.id, item]));
  const merged = reviewQueue
    .filter(item => fresh.has(item.id) && !pendingReviewIds.has(item.id))
    .map(item => fresh.get(item.id));
  const seen = new Set(merged.map(item => item.id));
  available.forEach(item => { if (!seen.has(item.id)) merged.push(item); });
  reviewQueue = merged;
}

async function refreshReviewBuffer() {
  if (reviewRefreshInFlight) return;
  reviewRefreshInFlight = true;
  try {
    await fetchLibrary({preserveReview: true, renderReviewView: false});
    if (!reviewQueue.length) renderReview(true);
    else preloadReviewQueue(5);
  } catch (_) {
    // Keep the already-loaded review buffer usable during a background refresh failure.
  } finally {
    reviewRefreshInFlight = false;
  }
}

function renderGate1bStatus() {
  const target = $('gate1b-safety');
  if (!target) return;
  const labels = n(gate1bMetrics.human_labels);
  const required = n(gate1bMetrics.minimum_labels);
  const recall = typeof gate1bMetrics.buck_recall === 'number'
    ? `${(100 * gate1bMetrics.buck_recall).toFixed(1)}%` : 'not measured';
  const suppression_ready = gate1bMetrics.suppression_ready === true;
  const suppression_enabled = gate1bMetrics.suppression_enabled === true;
  const coverage = `${n(gate1bMetrics.predictions)} model-labeled events · ${n(gate1bMetrics.prediction_cameras)}/4 cameras · ${n(gate1bMetrics.predicted_day)} day · ${n(gate1bMetrics.predicted_ir)} IR · ${n(gate1bMetrics.predicted_axis)} predicted axis`;
  target.className = 'gate1b-safety ' + (suppression_enabled ? 'enabled' : 'locked');
  target.textContent = suppression_enabled
    ? `Operational override active · female-only candidates filtered · likely male/antlers request HD automatically · ${coverage} · measured buck recall ${recall} · ${labels} human labels`
    : `Female-only suppression locked · ${coverage} · ${labels}/${required} human labels · buck recall ${recall}${suppression_ready ? ' · validation ready for explicit activation' : ''}`;
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
  deerProfiles.forEach(profile => profiles.set(String(profile.id), {
    id: profile.id,
    name: profile.display_name || 'Named deer',
    seasonYear: profile.season_year,
    photoCount: n(profile.photo_count),
    previewUrls: Array.isArray(profile.preview_urls) ? profile.preview_urls.slice(0, 5) : [],
    photos: []
  }));
  photos.forEach(item => photoAnimals(item).forEach(animal => {
    const key = String(animal.profile_id || animal.id || animal.name || animal.profile_name || '');
    if (!key) return;
    const existing = profiles.get(key) || {name: animal.display_name || animal.name || animal.profile_name || 'Named deer', photos: [], photoCount: 0};
    existing.photos.push(item);
    existing.photoCount = Math.max(existing.photoCount || 0, existing.photos.length);
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
    const previewUrls = (profile.previewUrls || []).slice(0, 5);
    if (previewUrls.length) {
      const strip = document.createElement('div');
      strip.className = 'profile-thumbnail-strip';
      previewUrls.forEach((url, index) => {
        const image = document.createElement('img');
        image.src = url;
        image.alt = `${profile.name} confirmed photo ${index + 1}`;
        image.loading = 'lazy';
        image.referrerPolicy = 'no-referrer';
        strip.appendChild(image);
      });
      card.appendChild(strip);
    }
    const meta = document.createElement('div');
    meta.className = 'photo-meta';
    const title = document.createElement('strong');
    title.textContent = profile.name;
    const copy = document.createElement('div');
    copy.className = 'photo-date';
    const count = Math.max(profile.photoCount || 0, profile.photos.length);
    copy.textContent = `${count} confirmed ${count === 1 ? 'photo' : 'photos'}${profile.seasonYear ? ` · ${profile.seasonYear}` : ''}`;
    meta.append(title, copy);
    card.appendChild(meta);
    target.appendChild(card);
  });
}

function renderAutomationAudit() {
  const target = $('automation-audit-grid');
  if (!target) return;
  target.replaceChildren();
  automationAudit.forEach(item => {
    const card=document.createElement('article'); card.className='card photo-card';
    const image=document.createElement('img'); image.src=item.preview_url; image.alt='Automatically routed thumbnail'; image.loading='lazy'; image.referrerPolicy='no-referrer';
    const meta=document.createElement('div'); meta.className='photo-meta';
    const title=document.createElement('strong'); title.textContent=item.action==='auto_request_hd'?'HD automatically requested':'Filtered as female-only';
    const copy=document.createElement('div'); copy.className='photo-date'; copy.textContent=`${item.camera_name||'Camera'} · ${formatDate(item.captured_at)}${item.human_verdict?` · ${item.human_verdict}`:''}`;
    const actions=document.createElement('div'); actions.className='quick-actions';
    const verdicts=item.action==='auto_request_hd'?[['correct','Correct'],['incorrect_male_or_antler','Incorrect male / antlers']]:[['correct','Correct'],['should_have_requested_hd','Should have requested HD']];
    verdicts.forEach(([verdict,label])=>{const button=document.createElement('button'); button.type='button'; button.textContent=label; button.addEventListener('click',async()=>{button.disabled=true; const response=await fetch('/api/automation_label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_token:item.action_token,verdict})}); const data=await response.json().catch(()=>({ok:false})); if(!response.ok||!data.ok){button.disabled=false;return showError('Audit label could not be saved.');} item.human_verdict=verdict; renderAutomationAudit();}); actions.appendChild(button);});
    meta.append(title,copy,actions); card.append(image,meta); target.appendChild(card);
  });
  if(!automationAudit.length){const empty=document.createElement('div');empty.className='card empty';empty.textContent='No automatic routing decisions yet.';target.appendChild(empty);}
}

function renderHDReview() {
  const target = $('hd-review-stage');
  if (!target) return;
  target.replaceChildren();
  hdReviewQueue.forEach(item => {
    const result = item.result || {};
    const bbox = item.bbox || {x: 0, y: 0, width: 1, height: 1};
    const card = document.createElement('article');
    card.className = 'card hd-review-card';

    const visual = document.createElement('div');
    visual.className = 'hd-instance-visual';
    const crop = document.createElement('div');
    crop.className = 'hd-instance-crop';
    const cropImage = document.createElement('img');
    cropImage.src = item.preview_url;
    cropImage.alt = `Animal ${item.instance_index} crop from returned HD photo`;
    cropImage.loading = 'lazy';
    cropImage.referrerPolicy = 'no-referrer';
    const setCropRatio = () => { crop.style.aspectRatio = `${Number(bbox.width || 1) * cropImage.naturalWidth} / ${Number(bbox.height || 1) * cropImage.naturalHeight}`; };
    cropImage.addEventListener('load', setCropRatio, {once: true});
    if (cropImage.complete && cropImage.naturalWidth) setCropRatio();
    cropImage.style.width = `${100 / Number(bbox.width || 1)}%`;
    cropImage.style.left = `${-100 * Number(bbox.x || 0) / Number(bbox.width || 1)}%`;
    cropImage.style.top = `${-100 * Number(bbox.y || 0) / Number(bbox.height || 1)}%`;
    crop.appendChild(cropImage);

    const context = document.createElement('div');
    context.className = 'hd-instance-context';
    const contextImage = document.createElement('img');
    contextImage.className = 'hd-review-image';
    contextImage.src = item.preview_url;
    contextImage.alt = 'Full returned HD frame for context';
    contextImage.loading = 'lazy';
    contextImage.referrerPolicy = 'no-referrer';
    const box = document.createElement('div');
    box.className = 'hd-context-box';
    box.style.left = `${100 * Number(bbox.x || 0)}%`;
    box.style.top = `${100 * Number(bbox.y || 0)}%`;
    box.style.width = `${100 * Number(bbox.width || 1)}%`;
    box.style.height = `${100 * Number(bbox.height || 1)}%`;
    context.append(contextImage, box);
    visual.append(crop, context);

    const meta = document.createElement('div');
    meta.className = 'photo-meta';
    const instance = document.createElement('div');
    instance.className = 'hd-instance-label';
    instance.textContent = `Animal ${item.instance_index} of ${item.instance_count} · One deer at a time`;
    const heading = document.createElement('strong');
    heading.textContent = `${result.species || 'Unknown deer'} · ${result.sex || 'unknown sex'}`;
    const summary = document.createElement('p');
    summary.textContent = result.summary || 'Analysis pending';
    const ageCues = (result.age_cues || []).join(', ') || 'not assessable';
    const details = document.createElement('p');
    details.textContent = `View: ${result.view_angle || 'unknown'} · visible tines L/R: ${result.visible_tines_left ?? '—'}/${result.visible_tines_right ?? '—'} · ${result.tine_count_limitations || 'visibility limitations not recorded'} · Antlers: ${result.antler_structure || 'not described'} · Age: ${result.age_class || 'unknown'} (${ageCues})`;
    const detection = document.createElement('p');
    detection.className = item.detection_complete ? 'hd-detection-ok' : 'hd-detection-warning';
    detection.textContent = item.detection_complete ? item.detection_notes : `Detector needs human attention: ${item.detection_notes}`;

    const controls = document.createElement('div');
    controls.className = 'profile-assignment';
    const select = document.createElement('select');
    deerProfiles.filter(p => Number(p.season_year) === new Date(item.captured_at).getFullYear()).forEach(p => {
      const option = document.createElement('option'); option.value = p.id; option.textContent = p.display_name; select.appendChild(option);
    });
    const match = document.createElement('button');
    match.textContent = 'Match this animal to existing profile';
    match.disabled = !select.options.length;
    match.onclick = () => submitHDReviewDecision(item, {action: 'match_profile', profile_id: select.value}, match);
    const name = document.createElement('input');
    name.placeholder = `New name for Animal ${item.instance_index}`;
    const create = document.createElement('button');
    create.textContent = 'Create profile for this animal';
    create.onclick = () => submitHDReviewDecision(item, {action: 'create_profile', display_name: name.value, species: result.species === 'axis' ? 'axis deer' : result.species === 'whitetail' ? 'white-tailed deer' : 'other deer', sex: ['male', 'female'].includes(result.sex) ? result.sex : 'unknown'}, create);
    const skip = document.createElement('button');
    skip.textContent = 'This animal is not identity-worthy';
    skip.onclick = () => submitHDReviewDecision(item, {action: 'not_identity_worthy'}, skip);
    controls.append(select, match, name, create, skip);
    meta.append(instance, heading, summary, details, detection, controls);
    card.append(visual, meta);
    target.appendChild(card);
  });
  if (!hdReviewQueue.length) {
    const empty = document.createElement('div'); empty.className = 'card empty'; empty.textContent = 'No returned HD animal instances awaiting profile review.'; target.appendChild(empty);
  }
}

async function submitHDReviewDecision(item, payload, button) {
  button.disabled = true;
  const response = await fetch('/api/hd_review_decision', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action_token: item.action_token, hd_animal_instance_id: item.hd_animal_instance_id, ...payload})});
  const data = await response.json().catch(() => ({ok: false}));
  if (!response.ok || !data.ok) { button.disabled = false; return showError('HD profile decision could not be saved.'); }
  hdReviewQueue = hdReviewQueue.filter(x => x.hd_animal_instance_id !== item.hd_animal_instance_id);
  await fetchLibrary();
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
  const allowed = ['overview', 'review', 'audit', 'hdreview', 'deer', 'cameras', 'photos'];
  if (!allowed.includes(name)) name = 'overview';
  activeView = name;
  document.querySelectorAll('[data-view-panel]').forEach(panel => { panel.hidden = panel.dataset.viewPanel !== name; });
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  history.replaceState(null, '', name === 'overview' ? location.pathname : `#${name}`);
  if (name === 'cameras') setTimeout(loadCameraMap, 0);
  window.scrollTo({top: 0, behavior: 'instant'});
}

function updateCatalogViews(renderReviewView = true) {
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
  const stats = operationalStats;
  $('photos-24h').textContent = n(stats.photos_received_24h);
  $('hd-requests-24h').textContent = n(stats.hd_requests_24h);
  $('hd-available-24h').textContent = n(stats.hd_available_24h);
  renderPipeline();
  renderGate1bStatus();
  renderPhotoGrid($('recent-photos'), photos.slice(0, 8), {emptyTitle: 'No cataloged photos yet'});
  populateCameraFilter();
  renderFilteredPhotos();
  if (renderReviewView) renderReview();
  else updateReviewCounts();
  renderDeerProfiles();
  renderCameraCards();
  renderAutomationAudit();
  renderHDReview();
  if (activeView === 'cameras') loadCameraMap();
}

async function fetchLibrary(options = {}) {
  const response = await fetch('/api/library', {cache: 'no-store'});
  const data = await response.json().catch(() => ({ok: false}));
  if (!response.ok || !data.ok) throw new Error('The photo catalog is temporarily unavailable.');
  photos = Array.isArray(data.photos) ? data.photos : [];
  cameras = Array.isArray(data.cameras) ? data.cameras : [];
  deerProfiles = Array.isArray(data.profiles) ? data.profiles : [];
  pipeline = data.pipeline && typeof data.pipeline === 'object' ? data.pipeline : {};
  gate1bMetrics = data.gate1b && typeof data.gate1b === 'object' ? data.gate1b : {};
  operationalStats = data.stats && typeof data.stats === 'object' ? data.stats : {};
  automationAudit = Array.isArray(data.automation_audit) ? data.automation_audit : [];
  hdReviewQueue = Array.isArray(data.hd_review_queue) ? data.hd_review_queue : [];
  mapboxToken = typeof data.mapbox_access_token === 'string' ? data.mapbox_access_token : '';
  mergeReviewQueue(photos.filter(belongsToActiveQueue), Boolean(options.preserveReview));
  updateCatalogViews(options.renderReviewView !== false);
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
  document.querySelectorAll('[data-review-queue]').forEach(button => button.addEventListener('click', () => {
    activeReviewQueue = button.dataset.reviewQueue;
    mergeReviewQueue(photos.filter(belongsToActiveQueue), false);
    renderReview(true);
  }));
  const requestedView = location.hash.slice(1);
  showView(requestedView || 'overview');
  const results = await Promise.allSettled([refreshStatus(), fetchLibrary()]);
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) showError(failures.map(result => result.reason.message).join(' '));
  $('loading-line').classList.add('done');
}

initialize();
setInterval(() => refreshStatus().catch(() => {}), 30000);
