-- Refresh every structured observation field during idempotent re-ingestion.

create or replace function public.deerid_ingest_reveal_batch(
  p_cameras jsonb,
  p_media jsonb,
  p_observed_at timestamptz default now()
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, deerid, pg_temp
as $$
declare
  c jsonb;
  item jsonb;
  photo jsonb;
  camera_row deerid.cameras%rowtype;
  media_row deerid.media%rowtype;
  camera_total integer := 0;
  media_total integer := 0;
  lat numeric;
  lon numeric;
  observed timestamptz;
  weather jsonb;
begin
  if jsonb_typeof(coalesce(p_cameras, '[]'::jsonb)) <> 'array'
     or jsonb_typeof(coalesce(p_media, '[]'::jsonb)) <> 'array' then
    raise exception 'catalog payloads must be arrays';
  end if;

  for c in select value from jsonb_array_elements(coalesce(p_cameras, '[]'::jsonb)) loop
    if nullif(c->>'cameraId', '') is null then
      continue;
    end if;

    insert into deerid.cameras (
      provider_camera_id, provider_account_id, name, location_name, postal_code,
      hardware_version, firmware_version, firmware_status, plan_name, carrier,
      activated_at, warranty_ends_at, last_seen_at, raw_payload
    ) values (
      c->>'cameraId', c->>'accountId', c->>'name', coalesce(c->>'cameraLocation', c->>'location'), coalesce(c->>'zip', c->>'zipCode'),
      c->>'hardwareVersion', coalesce(c->>'cameraFirmwareVersion', c->>'firmwareVersion', c#>>'{status,firmwareVersion}'),
      c->>'firmwareStatus', coalesce(c->>'planName', c->>'plan'), coalesce(c->>'carrier', c#>>'{status,carrier}'),
      deerid.try_timestamptz(c->>'firstActivationTime'), deerid.try_timestamptz(coalesce(c->>'cameraWarrantyEndDate', c->>'warrantyEndDate')),
      p_observed_at, c
    )
    on conflict (provider, provider_camera_id) do update set
      provider_account_id = excluded.provider_account_id,
      name = excluded.name,
      location_name = excluded.location_name,
      postal_code = excluded.postal_code,
      hardware_version = excluded.hardware_version,
      firmware_version = excluded.firmware_version,
      firmware_status = excluded.firmware_status,
      plan_name = excluded.plan_name,
      carrier = excluded.carrier,
      activated_at = coalesce(excluded.activated_at, deerid.cameras.activated_at),
      warranty_ends_at = coalesce(excluded.warranty_ends_at, deerid.cameras.warranty_ends_at),
      last_seen_at = excluded.last_seen_at,
      raw_payload = excluded.raw_payload
    returning * into camera_row;
    camera_total := camera_total + 1;

    observed := coalesce(
      deerid.try_timestamptz(c#>>'{gps,lastUpdatedTimestamp}'),
      deerid.try_timestamptz(c->>'updatedAt'),
      p_observed_at
    );
    lat := deerid.try_numeric(coalesce(c#>>'{gps,latitude}', c#>>'{gps,lat}'));
    lon := deerid.try_numeric(coalesce(c#>>'{gps,longitude}', c#>>'{gps,lon}'));
    if lat between -90 and 90 and lon between -180 and 180 then
      insert into deerid.camera_locations (camera_id, latitude, longitude, observed_at, source, raw_payload)
      values (camera_row.id, lat, lon, observed, 'provider_camera', coalesce(c->'gps', '{}'::jsonb))
      on conflict (camera_id, observed_at, source) do update set
        latitude = excluded.latitude, longitude = excluded.longitude, raw_payload = excluded.raw_payload;
    end if;

    if jsonb_typeof(c->'status') = 'object' or c ? 'batteryLevel' or c ? 'signal' then
      observed := coalesce(
        deerid.try_timestamptz(c#>>'{status,lastTransmissionTime}'),
        deerid.try_timestamptz(c->>'updatedAt'),
        p_observed_at
      );
      insert into deerid.camera_status_observations (
        camera_id, observed_at, battery_level, signal_level, temperature,
        memory_used, memory_limit, internal_voltage, external_voltage, voltage_source,
        solar_percent, sd_card_status, last_transmission_at, serving_cell, raw_payload
      ) values (
        camera_row.id, observed,
        deerid.try_numeric(coalesce(c#>>'{status,batteryLevel}', c->>'batteryLevel')), deerid.try_numeric(coalesce(c#>>'{status,signalLevel}', c->>'signalLevel', c->>'signal')),
        deerid.try_numeric(c#>>'{status,temperature}'), deerid.try_numeric(c#>>'{status,memory}'),
        deerid.try_numeric(c#>>'{status,memoryLimit}'), deerid.try_numeric(c#>>'{status,internalVoltage}'),
        deerid.try_numeric(c#>>'{status,externalVoltage}'), c#>>'{status,voltageSource}',
        deerid.try_numeric(c#>>'{status,solarBatteryPercent}'), coalesce(c#>>'{status,sdCard}', c#>>'{status,sdCardStatus}'),
        deerid.try_timestamptz(c#>>'{status,lastTransmissionTime}'), c#>>'{status,servingCell}', coalesce(c->'status', c)
      )
      on conflict (camera_id, observed_at) do update set
        battery_level = excluded.battery_level,
        signal_level = excluded.signal_level,
        temperature = excluded.temperature,
        memory_used = excluded.memory_used,
        memory_limit = excluded.memory_limit,
        internal_voltage = excluded.internal_voltage,
        external_voltage = excluded.external_voltage,
        voltage_source = excluded.voltage_source,
        solar_percent = excluded.solar_percent,
        sd_card_status = excluded.sd_card_status,
        last_transmission_at = excluded.last_transmission_at,
        serving_cell = excluded.serving_cell,
        raw_payload = excluded.raw_payload;
    end if;

    if jsonb_typeof(c->'settings') = 'array' then
      insert into deerid.camera_settings_snapshots (camera_id, observed_at, settings)
      values (camera_row.id, p_observed_at, c->'settings')
      on conflict (camera_id, observed_at) do update set settings = excluded.settings;
    end if;
  end loop;

  for item in select value from jsonb_array_elements(coalesce(p_media, '[]'::jsonb)) loop
    photo := item->'provider';
    if jsonb_typeof(photo) <> 'object'
       or nullif(photo->>'photoId', '') is null
       or nullif(photo->>'cameraId', '') is null
       or deerid.try_timestamptz(photo->>'photoDateUtc') is null
       or nullif(item->>'object_path', '') is null
       or coalesce(item->>'image_sha256', '') !~ '^[0-9a-f]{64}$' then
      raise exception 'invalid verified media catalog item';
    end if;

    insert into deerid.media (
      provider_photo_id, camera_id, provider_camera_id, captured_at, synchronized_at,
      media_type, ownership_type, variant, hd_photo, has_headshot, delay_syncing,
      battery_level, signal_level, object_path, image_sha256, image_bytes,
      width, height, content_type, filename, last_seen_at, raw_payload
    ) values (
      photo->>'photoId',
      (select id from deerid.cameras where provider = 'reveal' and provider_camera_id = photo->>'cameraId'),
      photo->>'cameraId', deerid.try_timestamptz(photo->>'photoDateUtc'),
      coalesce(deerid.try_timestamptz(photo->>'lastSynchronizedAt'), deerid.try_timestamptz(photo->>'lastSyncTime')),
      photo->>'type', photo->>'ownershipType',
      case when coalesce(deerid.try_numeric(photo->>'hdPhoto'), 0) <> 0 or lower(coalesce(photo->>'hdPhoto','')) = 'true'
        then 'cloud_hd' else 'cloud_thumbnail' end,
      case when jsonb_typeof(photo->'hdPhoto') = 'boolean' then (photo->>'hdPhoto')::boolean else null end,
      case when jsonb_typeof(photo->'hasHeadshot') = 'boolean' then (photo->>'hasHeadshot')::boolean else null end,
      case when jsonb_typeof(photo->'delaySyncing') = 'boolean' then (photo->>'delaySyncing')::boolean else null end,
      deerid.try_numeric(coalesce(photo#>>'{metadata,batteryLevel}', photo->>'batteryLevel')),
      deerid.try_numeric(coalesce(photo#>>'{metadata,signal}', photo#>>'{metadata,signalLevel}', photo->>'signalLevel', photo->>'signal')),
      item->>'object_path', item->>'image_sha256', (item->>'image_bytes')::bigint,
      nullif(item->>'width','')::integer, nullif(item->>'height','')::integer,
      item->>'content_type', photo->>'filename', p_observed_at, photo
    )
    on conflict (provider, provider_photo_id) do update set
      provider_camera_id = excluded.provider_camera_id,
      camera_id = excluded.camera_id,
      captured_at = excluded.captured_at,
      synchronized_at = coalesce(excluded.synchronized_at, deerid.media.synchronized_at),
      media_type = excluded.media_type,
      ownership_type = excluded.ownership_type,
      variant = excluded.variant,
      hd_photo = excluded.hd_photo,
      has_headshot = excluded.has_headshot,
      delay_syncing = excluded.delay_syncing,
      battery_level = excluded.battery_level,
      signal_level = excluded.signal_level,
      object_path = excluded.object_path,
      image_sha256 = excluded.image_sha256,
      image_bytes = excluded.image_bytes,
      width = excluded.width,
      height = excluded.height,
      content_type = excluded.content_type,
      filename = excluded.filename,
      last_seen_at = excluded.last_seen_at,
      raw_payload = excluded.raw_payload
    returning * into media_row;
    media_total := media_total + 1;

    lat := deerid.try_numeric(coalesce(
      photo#>>'{gpsLocation,lat}', photo#>>'{gpsLocation,latitude}',
      photo#>>'{gps,lat}', photo#>>'{gps,latitude}',
      photo#>>'{location,lat}', photo#>>'{location,latitude}',
      photo->>'latitude'
    ));
    lon := deerid.try_numeric(coalesce(
      photo#>>'{gpsLocation,lon}', photo#>>'{gpsLocation,longitude}',
      photo#>>'{gps,lon}', photo#>>'{gps,longitude}',
      photo#>>'{location,lon}', photo#>>'{location,longitude}',
      photo->>'longitude'
    ));
    if media_row.camera_id is not null and lat between -90 and 90 and lon between -180 and 180 then
      insert into deerid.camera_locations (camera_id, latitude, longitude, observed_at, source, raw_payload)
      values (
        media_row.camera_id, lat, lon, media_row.captured_at, 'provider_photo',
        coalesce(photo->'gpsLocation', photo->'gps', photo->'location', '{}'::jsonb)
      )
      on conflict (camera_id, observed_at, source) do update set
        latitude = excluded.latitude, longitude = excluded.longitude, raw_payload = excluded.raw_payload;
    end if;

    weather := coalesce(photo->'weatherRecord', photo->'weather', photo->'weatherData');
    if jsonb_typeof(weather) = 'object' then
      insert into deerid.media_weather (
        media_id, observed_at, condition, temperature, feels_like, humidity, pressure, pressure_tendency,
        minimum_temperature_12h, maximum_temperature_12h, temperature_departure_24h,
        wind_direction_degrees, wind_direction_short, wind_direction_long,
        wind_speed, wind_gust, moon_phase, sun_phase, raw_payload
      ) values (
        media_row.id, coalesce(deerid.try_timestamptz(weather->>'observationTime'), media_row.captured_at),
        coalesce(weather->>'weatherLabel', weather->>'condition', weather->>'weatherCondition'), deerid.try_numeric(weather->>'temperature'),
        deerid.try_numeric(weather->>'feelsLike'), deerid.try_numeric(weather->>'humidity'),
        deerid.try_numeric(coalesce(weather->>'barometricPressure', weather->>'pressure')), weather->>'pressureTendency',
        deerid.try_numeric(coalesce(weather#>>'{temperatureRange12Hours,min}', weather->>'minimumTemperature12Hour', weather->>'minTemperature12Hour')),
        deerid.try_numeric(coalesce(weather#>>'{temperatureRange12Hours,max}', weather->>'maximumTemperature12Hour', weather->>'maxTemperature12Hour')),
        deerid.try_numeric(weather->>'temperatureDeparture24Hour'),
        deerid.try_numeric(coalesce(weather#>>'{windDirection,degrees}', weather#>>'{windDirection,degree}', weather->>'windDirectionDegrees')),
        coalesce(weather#>>'{windDirection,cardinal}', weather->>'windDirectionShort'),
        coalesce(weather#>>'{windDirection,description}', weather->>'windDirectionLong'), deerid.try_numeric(weather->>'windSpeed'),
        deerid.try_numeric(weather->>'windGust'), weather->>'moonPhase', weather->>'sunPhase', weather
      )
      on conflict (media_id) do update set
        observed_at = excluded.observed_at,
        condition = excluded.condition,
        temperature = excluded.temperature,
        feels_like = excluded.feels_like,
        humidity = excluded.humidity,
        pressure = excluded.pressure,
        pressure_tendency = excluded.pressure_tendency,
        minimum_temperature_12h = excluded.minimum_temperature_12h,
        maximum_temperature_12h = excluded.maximum_temperature_12h,
        temperature_departure_24h = excluded.temperature_departure_24h,
        wind_direction_degrees = excluded.wind_direction_degrees,
        wind_direction_short = excluded.wind_direction_short,
        wind_direction_long = excluded.wind_direction_long,
        wind_speed = excluded.wind_speed,
        wind_gust = excluded.wind_gust,
        moon_phase = excluded.moon_phase,
        sun_phase = excluded.sun_phase,
        raw_payload = excluded.raw_payload;
    end if;

    insert into deerid.classification_jobs (media_id, stage, model_name, model_version, status, input_variant)
    values (media_row.id, 'triage', 'unassigned', null, 'pending', media_row.variant)
    on conflict (media_id, stage, model_name, model_version) do nothing;
  end loop;

  insert into deerid.ingestion_runs (
    started_at, finished_at, status, camera_count, media_count, details
  ) values (
    p_observed_at, now(), 'succeeded', camera_total, media_total,
    jsonb_build_object('catalog_version', 1)
  );

  return jsonb_build_object('ok', true, 'cameras', camera_total, 'media', media_total);
end;
$$;




revoke all on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) from public, anon, authenticated;
grant execute on function public.deerid_ingest_reveal_batch(jsonb, jsonb, timestamptz) to service_role;
