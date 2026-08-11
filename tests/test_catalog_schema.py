import json
from pathlib import Path
import unittest

from reveal_downloader.client import RevealError
from reveal_downloader.supabase import SupabaseArchive
from tests.test_supabase import FakeRevealClient, FakeStorageTransport


MIGRATIONS = Path("supabase/migrations")


class CatalogSchemaTests(unittest.TestCase):
    def test_timestamp_parser_is_correctly_marked_stable(self):
        migration = MIGRATIONS / "20260810232827_timestamp_parser_stability.sql"
        sql = migration.read_text(encoding="utf-8").lower()
        self.assertIn("alter function deerid.try_timestamptz(text) stable", sql)

    def test_review_fix_migration_handles_observed_reveal_shapes_and_local_collections(self):
        migration = MIGRATIONS / "20260810234439_catalog_review_fixes.sql"
        sql = migration.read_text(encoding="utf-8")
        lower = sql.lower()
        self.assertIn("drop constraint if exists collections_collection_type_provider_collection_id_key", lower)
        self.assertIn("where provider_collection_id is not null", lower)
        for field in (
            "cameraLocation",
            "cameraWarrantyEndDate",
            "{metadata,batteryLevel}",
            "{metadata,signal}",
            "weatherRecord",
            "weatherLabel",
            "barometricPressure",
            "windDirection",
            "temperatureRange12Hours",
        ):
            self.assertIn(field, sql)

    def test_top_level_camera_status_payload_is_never_inserted_as_null(self):
        migration = MIGRATIONS / "20260810234858_camera_status_raw_payload.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("coalesce(c->'status', c)", sql)

    def test_reingestion_refreshes_all_structured_status_and_weather_fields(self):
        migration = MIGRATIONS / "20260810235551_refresh_observation_fields.sql"
        sql = migration.read_text(encoding="utf-8")
        for assignment in (
            "battery_level = excluded.battery_level",
            "signal_level = excluded.signal_level",
            "internal_voltage = excluded.internal_voltage",
            "solar_percent = excluded.solar_percent",
            "pressure_tendency = excluded.pressure_tendency",
            "minimum_temperature_12h = excluded.minimum_temperature_12h",
            "maximum_temperature_12h = excluded.maximum_temperature_12h",
            "wind_direction_degrees = excluded.wind_direction_degrees",
            "wind_speed = excluded.wind_speed",
            "moon_phase = excluded.moon_phase",
        ):
            self.assertIn(assignment, sql)

    def test_latest_media_upsert_refreshes_mutable_identity_fields(self):
        sql = (
            MIGRATIONS / "20260811002525_refresh_media_identity_fields.sql"
        ).read_text(encoding="utf-8").lower()
        for field in (
            "provider_camera_id",
            "camera_id",
            "captured_at",
            "media_type",
            "ownership_type",
        ):
            self.assertIn(f"{field} = excluded.{field}", sql)

    def test_foundation_migration_defines_private_reveal_catalog(self):
        migrations = sorted(MIGRATIONS.glob("*_deerid_foundation.sql"))
        self.assertEqual(len(migrations), 1)
        sql = migrations[0].read_text(encoding="utf-8").lower()

        for table in (
            "deerid.cameras",
            "deerid.camera_locations",
            "deerid.camera_status_observations",
            "deerid.camera_settings_snapshots",
            "deerid.media",
            "deerid.media_weather",
            "deerid.media_labels",
            "deerid.animals",
            "deerid.animal_profiles",
            "deerid.animal_media",
            "deerid.collections",
            "deerid.collection_items",
            "deerid.classification_jobs",
            "deerid.ingestion_runs",
        ):
            self.assertIn("create table if not exists " + table, sql)
            self.assertIn("alter table " + table + " enable row level security", sql)

        self.assertIn("create or replace function public.deerid_ingest_reveal_batch", sql)
        self.assertIn("photo#>>'{gps,latitude}'", sql)
        self.assertIn("photo#>>'{gps,longitude}'", sql)
        self.assertIn("unique nulls not distinct (media_id, stage, model_name, model_version)", sql)
        self.assertIn("create or replace function public.deerid_private_library", sql)
        self.assertIn("create or replace function public.deerid_private_camera_map", sql)
        self.assertIn("create or replace function public.deerid_private_media_object", sql)
        self.assertIn("grant execute on function public.deerid_ingest_reveal_batch", sql)
        self.assertIn("to service_role", sql)
        self.assertNotIn("grant select on", sql)
        self.assertNotIn("to anon", sql)

        collection_items = sql.split(
            "create table if not exists deerid.collection_items", 1
        )[1].split(");", 1)[0]
        self.assertIn("id uuid primary key", collection_items)
        self.assertNotIn("primary key (collection_id, media_id, animal_id)", collection_items)
        self.assertIn("unique nulls not distinct (collection_id, media_id, animal_id)", collection_items)


class CatalogTransport(FakeStorageTransport):
    def __init__(self, *, rpc_status=200):
        super().__init__()
        self.rpc_status = rpc_status
        self.catalog_payloads = []
        self.catalog_headers = []

    def request(self, method, url, *, headers=None, body=None, max_response_bytes=None):
        if url.endswith("/rest/v1/rpc/deerid_ingest_reveal_batch"):
            self.calls.append((method, url, headers or {}, body))
            self.catalog_payloads.append(json.loads(body))
            self.catalog_headers.append(dict(headers or {}))
            from reveal_downloader.supabase import StorageResponse

            return StorageResponse(self.rpc_status, b'{"ok":true}')
        return super().request(
            method,
            url,
            headers=headers,
            body=body,
            max_response_bytes=max_response_bytes,
        )


class CameraRevealClient(FakeRevealClient):
    def __init__(self):
        super().__init__()
        self.camera_calls = 0

    def get_cameras(self):
        self.camera_calls += 1
        return [
            {
                "cameraId": "cam-1",
                "name": "North Ridge",
                "iccid": "carrier-secret",
                "fcmTokens": ["push-secret"],
                "gps": {"latitude": 30.1, "longitude": -100.2},
                "status": {"batteryLevel": 82, "signalLevel": 4},
                "settings": [{"code": "GPS", "value": "ON"}],
            }
        ]


class SupabaseCatalogTests(unittest.TestCase):
    def test_catalog_is_opt_in_and_does_not_add_upstream_calls_by_default(self):
        transport = CatalogTransport()
        client = CameraRevealClient()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )

        archive.sync(client, max_pages=1)

        self.assertEqual(client.camera_calls, 0)
        self.assertEqual(transport.catalog_payloads, [])

    def test_enabled_catalog_indexes_only_verified_archive_units(self):
        transport = CatalogTransport()
        client = CameraRevealClient()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(client, max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(client.camera_calls, 1)
        self.assertEqual(len(transport.catalog_payloads), 1)
        payload = transport.catalog_payloads[0]
        self.assertEqual(payload["p_cameras"][0]["cameraId"], "cam-1")
        self.assertEqual(payload["p_media"][0]["provider"]["photoId"], "p1")
        self.assertTrue(payload["p_media"][0]["object_path"].endswith(".jpg"))
        self.assertEqual(len(payload["p_media"][0]["image_sha256"]), 64)
        self.assertGreater(payload["p_media"][0]["image_bytes"], 0)
        serialized = json.dumps(payload)
        self.assertNotIn("SUPABASE_SECRET_KEY", serialized)
        self.assertNotIn("photoUrl", serialized)
        self.assertNotIn("carrier-secret", serialized)
        self.assertNotIn("push-secret", serialized)
        self.assertEqual(transport.catalog_headers[0].get("apikey"), "sb_secret_test")
        self.assertNotIn("Authorization", transport.catalog_headers[0])

    def test_catalog_failure_degrades_but_does_not_undo_verified_archive(self):
        transport = CatalogTransport(rpc_status=404)
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(CameraRevealClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.failure_stages, {"catalog_index": 1})
        self.assertEqual(len(archive.last_archive_units), 1)
        self.assertEqual(len(transport.catalog_payloads), 1)

    def test_camera_catalog_failure_does_not_block_photo_archive_or_media_index(self):
        class CameraFailureClient(CameraRevealClient):
            def get_cameras(self):
                raise RevealError("camera inventory unavailable")

        transport = CatalogTransport()
        archive = SupabaseArchive(
            "https://project.supabase.co", "sb_secret_test", transport=transport
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(CameraFailureClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(archive.failure_stages, {"catalog_cameras": 1})
        self.assertEqual(transport.catalog_payloads[0]["p_cameras"], [])
        self.assertEqual(len(transport.catalog_payloads[0]["p_media"]), 1)

    def test_unexpected_auxiliary_catalog_errors_never_mask_archive_success(self):
        class MalformedCameraClient(CameraRevealClient):
            def get_cameras(self):
                raise TypeError("malformed camera response")

        class BrokenCatalogTransport(CatalogTransport):
            def request(self, method, url, **kwargs):
                if url.endswith("/rest/v1/rpc/deerid_ingest_reveal_batch"):
                    raise ValueError("catalog serialization failed")
                return super().request(method, url, **kwargs)

        archive = SupabaseArchive(
            "https://project.supabase.co",
            "sb_secret_test",
            transport=BrokenCatalogTransport(),
        )
        archive.set_catalog_enabled(True)

        result = archive.sync(MalformedCameraClient(), max_pages=1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.failed, 2)
        self.assertEqual(archive.failure_stages, {
            "catalog_cameras": 1,
            "catalog_index": 1,
        })
        self.assertEqual(len(archive.last_archive_units), 1)


if __name__ == "__main__":
    unittest.main()
