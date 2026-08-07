# Reveal Cell Cam Home Assistant Integration

A comprehensive Home Assistant integration for Tactacam Reveal Cell Cam trail cameras, providing real-time access to photos and detailed statistics from all your cameras.

## Standalone Downloader (No Home Assistant Required)

This repository now includes a standalone, dependency-free downloader for archiving Reveal cloud photos and metadata directly to disk. See [DOWNLOADER.md](DOWNLOADER.md) for secure setup, commands, archive layout, and limitations.

## Features

### Camera Management
- **Automatic Discovery**: Automatically discovers and displays all cameras associated with your account
- **Multi-Camera Support**: View and manage multiple trail cameras from a single integration
- **Individual Camera Selection**: Select and view any camera individually in Home Assistant
- **Real-time Photos**: Display the latest photo from each camera with automatic caching

### Detailed Statistics & Metadata
Each camera entity includes comprehensive statistics:

**Camera Information:**
- Camera name and location
- Camera ID and status
- Hardware and firmware versions
- Total photo count
- First and last photo dates

**Current Status:**
- Battery level (current and average)
- Signal strength (1-5 bars, current and average)
- GPS coordinates (if available)
- Last photo timestamp and filename

**Weather Data from Camera Location:**
- Current temperature and weather conditions
- Wind speed, direction, and gusts
- Barometric pressure and pressure tendency
- Moon phase and sun phase
- 12-hour temperature range (min/max)
- 24-hour temperature departure

### Authentication & Security
- **AWS Cognito Authentication**: Secure authentication using AWS Cognito
- **Automatic Token Management**: Handles token refresh automatically
- **Session Management**: Maintains persistent sessions for reliable connectivity

### Update & Refresh
- **Automatic Updates**: Refreshes camera data every 5 minutes by default
- **Manual Refresh**: Service calls available for on-demand updates
- **Smart Caching**: Efficiently caches images to reduce API calls

## Installation

### Manual Installation

1. Copy the `custom_components/reveal_cell_cam` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant
3. Go to Settings → Devices & Services
4. Click "Add Integration"
5. Search for "Reveal Cell Cam"
6. Enter your Reveal Cell Cam account credentials

### HACS Installation (Coming Soon)

This integration will be available through HACS in the future.

## Configuration

The integration is configured through the Home Assistant UI. You'll need:

- Your Reveal Cell Cam account email/username
- Your Reveal Cell Cam account password

## Entities

For each camera in your account, the integration creates a camera entity with the following structure:

**Entity ID**: `camera.reveal_[camera_name]`

### Available Attributes

| Attribute | Description | Example |
|-----------|-------------|---------|
| `camera_id` | Unique camera identifier | "016578004295819" |
| `camera_name` | Camera display name | "CAM01" |
| `location` | Camera location/description | "North Field" |
| `status` | Camera status | "active" |
| `total_photos` | Total photos taken | 1543 |
| `battery_level` | Current battery percentage | "94" |
| `signal_strength` | Cell signal (1-5) | "4" |
| `temperature` | Current temperature (°F) | 74 |
| `weather` | Weather conditions | "Mostly sunny" |
| `moon_phase` | Current moon phase | "Waxing Crescent" |
| `wind_speed` | Wind speed (mph) | 4.4 |
| `wind_direction` | Wind direction | "NW" |
| `barometric_pressure` | Pressure (inHg) | 30.21 |
| `gps_coordinates` | GPS location | "00.90803, -00.92407" |
| `last_photo_time` | Last photo timestamp | "2025-08-26T21:31:20.000Z" |
| `average_battery` | Average battery level | 95.5 |
| `average_signal` | Average signal strength | 3.8 |

## Camera Gallery View

To create a dashboard showing all your cameras:

```yaml
type: grid
cards:
  - type: picture-entity
    entity: camera.reveal_cam01
    name: North Field Camera
    camera_view: live
    show_state: false
  - type: picture-entity
    entity: camera.reveal_south_cam
    name: South Trail Camera
    camera_view: live
    show_state: false
```

## Example Automations

### Send notification when new photo is captured

```yaml
automation:
  - alias: "Trail Camera Motion Alert"
    trigger:
      - platform: state
        entity_id: camera.reveal_camera_cam01
        attribute: last_photo_time
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Trail Camera Alert"
          message: "New photo captured at {{ state_attr('camera.reveal_camera_cam01', 'last_photo_time') }}"
          data:
            image: "/api/camera_proxy/camera.reveal_camera_cam01"
```

### Low battery warning

```yaml
automation:
  - alias: "Trail Camera Low Battery"
    trigger:
      - platform: numeric_state
        entity_id: camera.reveal_camera_cam01
        attribute: battery_level
        below: 20
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Trail Camera Battery Low"
          message: "Battery level is {{ state_attr('camera.reveal_camera_cam01', 'battery_level') }}%"
```

## Troubleshooting

### Authentication Issues

The integration uses the same API endpoints as the Reveal Cell Cam web interface. If you're having authentication issues:

1. Verify you can log into the web interface at https://account.revealcellcam.com
2. Check your username and password are correct
3. Ensure your account is active and has cameras associated with it

### No Images Displaying

If cameras are discovered but images aren't displaying:

1. Check the Home Assistant logs for errors
2. Verify the cameras have recent photos in the web interface
3. The integration only displays photos that have been synced to the cloud

## Known Limitations

- The integration uses the web API which may not have full authentication implementation yet
- Photos are fetched from pre-signed S3 URLs which expire after 7 days
- Update interval is fixed at 5 minutes to avoid excessive API calls

## Support

For issues and feature requests, please open an issue on the GitHub repository.

## Disclaimer

This is an unofficial integration and is not affiliated with or endorsed by Tactacam or Reveal.
