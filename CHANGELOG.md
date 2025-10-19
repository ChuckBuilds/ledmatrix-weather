# Changelog

## [2.0.8] - 2025-10-19

### Fixed
- **CRITICAL**: Added missing `class_name` field to manifest
  - Plugin system now correctly identifies the Python class to load
  - Fixes "No class_name in manifest" error

## [2.0.7] - 2025-10-19

### Removed
- Removed redundant `enabled` field from config schema
  - Plugin enabled state is now managed solely by the plugin system
  - This eliminates confusion from having two "enabled" toggles in the UI

### Fixed
- Configuration UI no longer shows duplicate enabled toggle
- Reduced debug log verbosity - removed noisy hourly state comparison logs

## [2.0.6] - 2025-10-19

### Changed
- Comprehensive weather display with current conditions, hourly forecast, and daily forecast
- UV index display
- Wind direction
- Weather icons
- State caching
- API counter tracking
- Error handling

