-- Avoid the pathological generic plan chosen for optional All Photos filters.
-- Parameter-aware planning keeps the bounded archive page inside the serverless deadline.
alter function public.deerid_all_photos(
  integer,
  text,
  date,
  date,
  integer,
  integer,
  text,
  uuid,
  text,
  text,
  text,
  text,
  text,
  text
) set plan_cache_mode = 'force_custom_plan';
