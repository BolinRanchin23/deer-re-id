# Reveal tags and Hit Lists — capability note

Investigated 2026-08-10 against Reveal Web 5.10.0, its public bundle, and current official Tactacam support articles. Reveal remains an unofficial integration surface; endpoints can change without notice.

## Verdict

| Capability | Verdict | Evidence |
|---|---|---|
| Read standard photo tags | **SOLVED** | `GET /v1/photo-tags`; UI uses `photoTags[]` fields `tagId`, `name`, and `sortOrder`. |
| Apply/remove a standard tag | **SOLVED** | `PUT /v1/photos/{photoId}` with `{"tagIds":["..."]}`; removal uses an empty array. Videos cannot currently be tagged. |
| Create arbitrary tags | **NOT SUPPORTED BY OBSERVED SURFACE** | No tag-catalog create/update/delete mutation exists; official instructions select from provider-defined tags. |
| Read ordinary galleries | **SOLVED** | `GET /v1/photoGroups?galleryType=standard`. |
| Create/update/delete ordinary galleries | **SOLVED** | Generic photo-group endpoints listed below. |
| Read/create Hit Lists externally | **HARD-BUT-DOABLE, exact target payload unverified** | Official product docs prove Hit List CRUD. Bundle enum defines group type `target`, but current web calls only `standard`; mobile target payload was not captured. |
| Assign photos to a group | **SOLVED for generic groups** | `POST /v1/photoGroups/{id}/batchAdd` with `{"photoIds":[...]}`. Exact reuse for target Hit Lists is likely but not yet captured. |
| Shared-camera guest sees owner tags | **NO in observed shared-feed UI** | Shared feed sets `hidePhotoTags: true` and skips the tag-catalog request. Universal mobile/raw-payload behavior remains unverified. |
| Shared-camera guest sees owner Hit Lists | **Strongly no** | Shared feed uses `/shared-cameras/photos`, not `/photoGroups`, hides gallery controls, and official docs say recipients cannot create galleries from shared-camera photos. |
| Explicitly share a Hit List | **Face Card export only** | Official docs support sharing/downloading Face Cards; that is not synchronized guest access to the underlying Hit List. |

## Exact observed API surface

Base URL:

```text
https://api.reveal.ishareit.net/v1
```

### Tags

```text
GET /photo-tags
GET /photos/{photoId}
GET /photos/v2
PUT /photos/{photoId}
```

Observed photo fields include `tagIds[]` and `photoGroupIds[]`. The current provider tag vocabulary recognized by the web app is:

```text
Bear, Bird, Buck, Doe, Human, No Animals, Other,
Predator, Turkey, Vehicle/ATV
```

IDs must always be loaded from `GET /photo-tags`; names must not be hard-coded as identifiers. No confidence score, bounding box, classifier version, or taxonomy provenance is exposed with these tags.

### Photo groups and probable Hit List backing model

```text
GET    /photoGroups
PUT    /photoGroups
POST   /photoGroups/{photoGroupId}
DELETE /photoGroups/{photoGroupId}
POST   /photoGroups/{photoGroupId}/batchAdd
DELETE /photoGroups/{photoGroupId}/batchDelete
GET    /photos/v2?photoGroupId={photoGroupId}
```

The bundle defines:

```text
PhotoGroupType: standard | protected | huntSync | target
TargetBuckStatus: active | harvested | m.i.a.
```

The current web wrapper performs:

```http
GET /photoGroups?galleryType=standard
```

A likely Hit List read is therefore:

```http
GET /photoGroups?galleryType=target
```

That exact query and the create/update fields for target status, Face Card crop, and favorites are **unverified**. Production automation must not guess `{"galleryType":"target"}` until one owner-authorized mobile trace confirms it.

## Sharing behavior

The shared-camera feed uses:

```http
GET /shared-cameras/photos?sharedCameraIds=...&size=100&page=0
```

Its presentation explicitly uses:

```text
hidePhotoTags = true
hidePhotoData = true
hideStar = true
```

Camera sharing should therefore be treated as permission to view a restricted media feed—not as access to the owner’s tag catalog, galleries, Hit Lists, DeerID profiles, or detailed classification evidence.

## DeerID product decision

1. Supabase is authoritative for animal identities, season-scoped profiles, match evidence, confidence, human confirmation, merges/splits, labels, and collections.
2. Reveal remains useful as the existing user/camera-sharing interface.
3. DeerID may later mirror a **coarse fixed Reveal tag** after human approval.
4. Generic gallery assignment is known, but Hit List write-back remains disabled until an owner-authorized mobile request confirms target payloads.
5. Friends using ordinary shared-camera access should not be expected to see owner tags or Hit Lists. If they need DeerID organization, use the authenticated DeerID library or an explicit Face Card export.
6. Before enabling write-back, run a reversible two-account test on one sacrificial photo: read tags, apply one tag, check owner and guest web/mobile visibility, remove it, and retain only redacted request/response shapes.

No authenticated tag, gallery, or Hit List write was made during this investigation.

## Sources

1. [Reveal web application](https://account.revealcellcam.com/)
2. [Reveal Web 5.10.0 bundle](https://account.revealcellcam.com/assets/index-CQDt0Fhq.js)
3. [Tactacam: How to Create and Manage a Hit List](https://tactacam.zendesk.com/api/v2/help_center/en-us/articles/37456836958619.json)
4. [Tactacam: Managing Photo Tags in the Reveal App](https://tactacam.zendesk.com/api/v2/help_center/en-us/articles/37145215712923.json)
5. [Tactacam: Sharing Your REVEAL Cameras with Other Users](https://tactacam.zendesk.com/api/v2/help_center/en-us/articles/37080696836891.json)
6. [Tactacam: Using Tactagroups for Group Camera Sharing](https://tactacam.zendesk.com/api/v2/help_center/en-us/articles/40239067009563.json)
7. [REVEAL app and plans](https://www.tactacam.com/reveal-app-plan)
