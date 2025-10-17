"""
Weather Plugin for LEDMatrix

Comprehensive weather display with current conditions, hourly forecast, and daily forecast.
Uses OpenWeatherMap API to provide accurate weather information with beautiful icons.

Features:
- Current weather conditions with temperature, humidity, wind speed
- Hourly forecast (next 24-48 hours)
- Daily forecast (next 7 days)
- Weather icons matching conditions
- UV index display
- Automatic error handling and retry logic

API Version: 1.0.0
"""

import logging
import requests
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
from PIL import Image, ImageDraw
from pathlib import Path

from src.plugin_system.base_plugin import BasePlugin

# Import weather icons if available
try:
    from src.weather_icons import WeatherIcons
except ImportError:
    # Fallback if weather icons not available
    class WeatherIcons:
        @staticmethod
        def draw_weather_icon(image, icon_code, x, y, size):
            # Simple fallback - just draw a circle
            draw = ImageDraw.Draw(image)
            draw.ellipse([x, y, x + size, y + size], outline=(255, 255, 255), width=2)

# Import API counter function
try:
    from web_interface_v2 import increment_api_counter
except ImportError:
    def increment_api_counter(kind: str, count: int = 1):
        pass

logger = logging.getLogger(__name__)


class WeatherPlugin(BasePlugin):
    """
    Weather plugin that displays current conditions and forecasts.
    
    Supports three display modes:
    - weather: Current conditions
    - hourly_forecast: Hourly forecast for next 48 hours
    - daily_forecast: Daily forecast for next 7 days
    
    Configuration options:
        api_key (str): OpenWeatherMap API key
        location (dict): City, state, country for weather data
        units (str): 'imperial' (F) or 'metric' (C)
        update_interval (int): Seconds between API updates
        display_modes (dict): Enable/disable specific display modes
    """
    
    def __init__(self, plugin_id: str, config: Dict[str, Any],
                 display_manager, cache_manager, plugin_manager):
        """Initialize the weather plugin."""
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        
        # Weather configuration
        self.api_key = config.get('api_key', 'YOUR_OPENWEATHERMAP_API_KEY')
        
        # Location - read from flat format (location_city, location_state, location_country)
        # These are the fields defined in config_schema.json for the web interface
        self.location = {
            'city': config.get('location_city', 'Dallas'),
            'state': config.get('location_state', 'Texas'),
            'country': config.get('location_country', 'US')
        }
        
        self.units = config.get('units', 'imperial')
        
        # Handle update_interval - ensure it's an int
        update_interval = config.get('update_interval', 1800)
        try:
            self.update_interval = int(update_interval)
        except (ValueError, TypeError):
            self.update_interval = 1800
        
        # Display modes - read from flat boolean fields
        # These are the fields defined in config_schema.json for the web interface
        self.show_current = config.get('show_current_weather', True)
        self.show_hourly = config.get('show_hourly_forecast', True)
        self.show_daily = config.get('show_daily_forecast', True)
        
        # Data storage
        self.weather_data = None
        self.forecast_data = None
        self.hourly_forecast = None
        self.daily_forecast = None
        self.last_update = 0
        
        # Error handling and throttling
        self.consecutive_errors = 0
        self.last_error_time = 0
        self.error_backoff_time = 60
        self.max_consecutive_errors = 5
        self.error_log_throttle = 300  # Only log errors every 5 minutes
        self.last_error_log_time = 0
        
        # State caching for display optimization
        self.last_weather_state = None
        self.last_hourly_state = None
        self.last_daily_state = None
        self.current_display_mode = None  # Track current mode to detect switches
        
        # Layout constants
        self.PADDING = 1
        self.ICON_SIZE = {
            'extra_large': 40,
            'large': 30,
            'medium': 24,
            'small': 14
        }
        self.COLORS = {
            'text': (255, 255, 255),
            'highlight': (255, 200, 0),
            'separator': (64, 64, 64),
            'temp_high': (255, 100, 100),
            'temp_low': (100, 100, 255),
            'dim': (180, 180, 180),
            'extra_dim': (120, 120, 120),
            'uv_low': (0, 150, 0),
            'uv_moderate': (255, 200, 0),
            'uv_high': (255, 120, 0),
            'uv_very_high': (200, 0, 0),
            'uv_extreme': (150, 0, 200)
        }
        
        # Weather icons path
        self.icons_dir = Path('assets/weather')
        
        # Register fonts
        self._register_fonts()
        
        self.logger.info(f"Weather plugin initialized for {self.location.get('city', 'Unknown')}")
        self.logger.info(f"Units: {self.units}, Update interval: {self.update_interval}s")
    
    def _register_fonts(self):
        """Register fonts with the font manager."""
        try:
            if not hasattr(self.plugin_manager, 'font_manager') or self.plugin_manager.font_manager is None:
                self.logger.warning("Font manager not available")
                return
            
            font_manager = self.plugin_manager.font_manager
            
            # Register fonts for different elements
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.temperature",
                family="press_start",
                size_px=16,
                color=self.COLORS['text']
            )
            
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.condition",
                family="four_by_six",
                size_px=8,
                color=self.COLORS['highlight']
            )
            
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.forecast_label",
                family="four_by_six",
                size_px=6,
                color=self.COLORS['dim']
            )
            
            self.logger.info("Weather plugin fonts registered successfully")
        except Exception as e:
            self.logger.warning(f"Error registering fonts: {e}")
    
    def update(self) -> None:
        """
        Update weather data from OpenWeatherMap API.
        
        Fetches current conditions and forecast data, respecting
        update intervals and error backoff periods.
        """
        current_time = time.time()
        
        # Check if we need to update
        if current_time - self.last_update < self.update_interval:
            return
        
        # Check if we're in error backoff period
        if self.consecutive_errors >= self.max_consecutive_errors:
            if current_time - self.last_error_time < self.error_backoff_time:
                self.logger.debug(f"In error backoff period, retrying in {self.error_backoff_time - (current_time - self.last_error_time):.0f}s")
                return
            else:
                # Reset error count after backoff
                self.consecutive_errors = 0
                self.error_backoff_time = 60
        
        # Validate API key
        if not self.api_key or self.api_key == "YOUR_OPENWEATHERMAP_API_KEY":
            self.logger.warning("No valid OpenWeatherMap API key configured")
            return
        
        # Try to fetch weather data
        try:
            self._fetch_weather()
            self.last_update = current_time
            self.consecutive_errors = 0
        except Exception as e:
            self.consecutive_errors += 1
            self.last_error_time = current_time
            
            # Exponential backoff: double the backoff time (max 1 hour)
            self.error_backoff_time = min(self.error_backoff_time * 2, 3600)
            
            # Only log errors periodically to avoid spam
            if current_time - self.last_error_log_time > self.error_log_throttle:
                self.logger.error(f"Error updating weather (attempt {self.consecutive_errors}/{self.max_consecutive_errors}): {e}")
                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.logger.error(f"Weather API disabled for {self.error_backoff_time} seconds due to repeated failures")
                self.last_error_log_time = current_time
    
    def _fetch_weather(self) -> None:
        """Fetch weather data from OpenWeatherMap API."""
        # Check cache first
        cache_key = 'weather'
        cached_data = self.cache_manager.get(cache_key)
        if cached_data:
            self.weather_data = cached_data.get('current')
            self.forecast_data = cached_data.get('forecast')
            if self.weather_data and self.forecast_data:
                self._process_forecast_data(self.forecast_data)
                self.logger.info("Using cached weather data")
                return
        
        # Fetch fresh data
        city = self.location.get('city', 'Dallas')
        state = self.location.get('state', 'Texas')
        country = self.location.get('country', 'US')
        
        # Get coordinates using geocoding API
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city},{state},{country}&limit=1&appid={self.api_key}"
        
        response = requests.get(geo_url, timeout=10)
        response.raise_for_status()
        geo_data = response.json()
        
        # Increment API counter for geocoding call
        increment_api_counter('weather', 1)
        
        if not geo_data:
            self.logger.error(f"Could not find coordinates for {city}, {state}")
            return
        
        lat = geo_data[0]['lat']
        lon = geo_data[0]['lon']
        
        # Get weather data using One Call API
        one_call_url = f"https://api.openweathermap.org/data/3.0/onecall?lat={lat}&lon={lon}&exclude=minutely,alerts&appid={self.api_key}&units={self.units}"
        
        response = requests.get(one_call_url, timeout=10)
        response.raise_for_status()
        one_call_data = response.json()
        
        # Increment API counter for weather data call
        increment_api_counter('weather', 1)
        
        # Store current weather data
        self.weather_data = {
            'main': {
                'temp': one_call_data['current']['temp'],
                'temp_max': one_call_data['daily'][0]['temp']['max'],
                'temp_min': one_call_data['daily'][0]['temp']['min'],
                'humidity': one_call_data['current']['humidity'],
                'pressure': one_call_data['current']['pressure'],
                'uvi': one_call_data['current'].get('uvi', 0)
            },
            'weather': one_call_data['current']['weather'],
            'wind': {
                'speed': one_call_data['current']['wind_speed']
            }
        }
        
        # Store forecast data
        self.forecast_data = one_call_data
        
        # Process forecast data
        self._process_forecast_data(self.forecast_data)
        
        # Cache the data
        self.cache_manager.set(cache_key, {
            'current': self.weather_data,
            'forecast': self.forecast_data
        })
        
        self.logger.info(f"Weather data updated for {city}: {self.weather_data['main']['temp']}°")
    
    def _process_forecast_data(self, forecast_data: Dict) -> None:
        """Process forecast data into hourly and daily lists."""
        if not forecast_data:
            return

        # Process hourly forecast (next 5 hours)
        hourly_list = forecast_data.get('hourly', [])[:5]
        self.hourly_forecast = []
        
        for hour_data in hourly_list:
            dt = datetime.fromtimestamp(hour_data['dt'])
            temp = round(hour_data['temp'])
            condition = hour_data['weather'][0]['main']
            icon_code = hour_data['weather'][0]['icon']
            self.hourly_forecast.append({
                'hour': dt.strftime('%I:00 %p').lstrip('0'),  # Format as "2:00 PM"
                'temp': temp,
                'condition': condition,
                'icon': icon_code
            })

        # Process daily forecast
        daily_list = forecast_data.get('daily', [])[1:4]  # Skip today (index 0) and get next 3 days
        self.daily_forecast = []
        
        for day_data in daily_list:
            dt = datetime.fromtimestamp(day_data['dt'])
            temp_high = round(day_data['temp']['max'])
            temp_low = round(day_data['temp']['min'])
            condition = day_data['weather'][0]['main']
            icon_code = day_data['weather'][0]['icon']
            
            self.daily_forecast.append({
                'date': dt.strftime('%a'),  # Day name (Mon, Tue, etc.)
                'date_str': dt.strftime('%m/%d'),  # Date (4/8, 4/9, etc.)
                'temp_high': temp_high,
                'temp_low': temp_low,
                'condition': condition,
                'icon': icon_code
            })
    
    def display(self, display_mode: str = None, force_clear: bool = False) -> None:
        """
        Display weather information.
        
        Args:
            display_mode: One of 'weather', 'hourly_forecast', 'daily_forecast', or display controller modes
            force_clear: If True, clear the display before rendering (ignored, kept for compatibility)
        """
        if not self.weather_data:
            self._display_no_data()
            return
        
        # Note: force_clear is handled by display_manager, not needed here
        # This parameter is kept for compatibility with BasePlugin interface
        
        # Map display controller modes to internal plugin modes
        mode_mapping = {
            'weather_current': 'weather',
            'weather_hourly': 'hourly_forecast',
            'weather_daily': 'daily_forecast'
        }
        
        # Convert display controller mode to plugin mode if needed
        if display_mode in mode_mapping:
            display_mode = mode_mapping[display_mode]
        
        # Check if mode has changed - if so, reset state cache to force redraw
        if display_mode != self.current_display_mode:
            if display_mode == 'hourly_forecast':
                self.last_hourly_state = None
            elif display_mode == 'daily_forecast':
                self.last_daily_state = None
            else:
                self.last_weather_state = None
            self.current_display_mode = display_mode
        
        # Determine which mode to display
        if display_mode == 'hourly_forecast' and self.show_hourly:
            self._display_hourly_forecast()
        elif display_mode == 'daily_forecast' and self.show_daily:
            self._display_daily_forecast()
        else:
            self._display_current_weather()
    
    def _display_no_data(self) -> None:
        """Display a message when no weather data is available."""
        img = Image.new('RGB', (self.display_manager.matrix.width, self.display_manager.matrix.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Simple text display
        from PIL import ImageFont
        try:
            font = ImageFont.truetype('assets/fonts/4x6-font.ttf', 8)
        except:
            font = ImageFont.load_default()
        
        draw.text((5, 12), "No Weather", font=font, fill=(200, 200, 200))
        draw.text((5, 20), "Data", font=font, fill=(200, 200, 200))
        
        self.display_manager.image = img
        self.display_manager.update_display()
    
    def _display_current_weather(self) -> None:
        """Display current weather conditions using comprehensive layout with icons."""
        try:
            # Check if state has changed
            current_state = self._get_weather_state()
            if current_state == self.last_weather_state:
                return  # No need to redraw if nothing changed

            # Clear the display
            self.display_manager.clear()
            
            # Create a new image for drawing
            img = Image.new('RGB', (self.display_manager.matrix.width, self.display_manager.matrix.height), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            # Get weather info
            temp = int(self.weather_data['main']['temp'])
            condition = self.weather_data['weather'][0]['main']
            icon_code = self.weather_data['weather'][0]['icon']
            humidity = self.weather_data['main']['humidity']
            pressure = self.weather_data['main']['pressure']
            wind_speed = self.weather_data['wind'].get('speed', 0)
            wind_deg = self.weather_data['wind'].get('deg', 0)  # Wind direction not always provided
            uv_index = self.weather_data['main'].get('uvi', 0)
            
            # Get daily high/low from the first day of forecast
            temp_high = int(self.weather_data['main']['temp_max'])
            temp_low = int(self.weather_data['main']['temp_min'])
            
            # --- Top Left: Weather Icon ---
            icon_size = self.ICON_SIZE['extra_large']
            icon_x = 1
            # Center the icon vertically in the top two-thirds of the display
            available_height = (self.display_manager.matrix.height * 2) // 3
            icon_y = (available_height - icon_size) // 2
            WeatherIcons.draw_weather_icon(img, icon_code, icon_x, icon_y, size=icon_size)
            
            # --- Top Right: Condition Text ---
            condition_font = self.display_manager.small_font
            condition_text_width = draw.textlength(condition, font=condition_font)
            condition_x = self.display_manager.matrix.width - condition_text_width - 1
            condition_y = 1
            draw.text((condition_x, condition_y), condition, font=condition_font, fill=self.COLORS['text'])

            # --- Right Side: Current Temperature ---
            temp_text = f"{temp}°"
            temp_font = self.display_manager.small_font
            temp_text_width = draw.textlength(temp_text, font=temp_font)
            temp_x = self.display_manager.matrix.width - temp_text_width - 1
            temp_y = condition_y + 8
            draw.text((temp_x, temp_y), temp_text, font=temp_font, fill=self.COLORS['highlight'])
            
            # --- Right Side: High/Low Temperature ---
            high_low_text = f"{temp_low}°/{temp_high}°"
            high_low_font = self.display_manager.small_font
            high_low_width = draw.textlength(high_low_text, font=high_low_font)
            high_low_x = self.display_manager.matrix.width - high_low_width - 1
            high_low_y = temp_y + 8
            draw.text((high_low_x, high_low_y), high_low_text, font=high_low_font, fill=self.COLORS['dim'])
            
            # --- Bottom: Additional Metrics ---
            display_width = self.display_manager.matrix.width
            section_width = display_width // 3
            y_pos = self.display_manager.matrix.height - 7
            font = self.display_manager.extra_small_font

            # --- UV Index (Section 1) ---
            uv_prefix = "UV:"
            uv_value_text = f"{uv_index:.0f}"
            
            prefix_width = draw.textlength(uv_prefix, font=font)
            value_width = draw.textlength(uv_value_text, font=font)
            total_width = prefix_width + value_width
            
            start_x = (section_width - total_width) // 2
            
            # Draw "UV:" prefix
            draw.text((start_x, y_pos), uv_prefix, font=font, fill=self.COLORS['dim'])

            # Draw UV value with color
            uv_color = self._get_uv_color(uv_index)
            draw.text((start_x + prefix_width, y_pos), uv_value_text, font=font, fill=uv_color)
            
            # --- Humidity (Section 2) ---
            humidity_text = f"H:{humidity}%"
            humidity_width = draw.textlength(humidity_text, font=font)
            humidity_x = section_width + (section_width - humidity_width) // 2
            draw.text((humidity_x, y_pos), humidity_text, font=font, fill=self.COLORS['dim'])

            # --- Wind (Section 3) ---
            wind_dir = self._get_wind_direction(wind_deg)
            wind_text = f"W:{wind_speed:.0f}{wind_dir}"
            wind_width = draw.textlength(wind_text, font=font)
            wind_x = (2 * section_width) + (section_width - wind_width) // 2
            draw.text((wind_x, y_pos), wind_text, font=font, fill=self.COLORS['dim'])
            
            # Update the display
            self.display_manager.image = img
            self.display_manager.update_display()
            self.last_weather_state = current_state

        except Exception as e:
            self.logger.error(f"Error displaying current weather: {e}")
    
    def _get_wind_direction(self, degrees: float) -> str:
        """Convert wind degrees to cardinal direction."""
        directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        index = round(degrees / 45) % 8
        return directions[index]

    def _get_uv_color(self, uv_index: float) -> tuple:
        """Get color based on UV index value."""
        if uv_index <= 2:
            return self.COLORS['uv_low']
        elif uv_index <= 5:
            return self.COLORS['uv_moderate']
        elif uv_index <= 7:
            return self.COLORS['uv_high']
        elif uv_index <= 10:
            return self.COLORS['uv_very_high']
        else:
            return self.COLORS['uv_extreme']
    
    def _get_weather_state(self) -> Dict[str, Any]:
        """Get current weather state for comparison."""
        if not self.weather_data:
            return None
        return {
            'temp': round(self.weather_data['main']['temp']),
            'condition': self.weather_data['weather'][0]['main'],
            'humidity': self.weather_data['main']['humidity'],
            'uvi': self.weather_data['main'].get('uvi', 0)
        }

    def _get_hourly_state(self) -> List[Dict[str, Any]]:
        """Get current hourly forecast state for comparison."""
        if not self.hourly_forecast:
            return None
        return [
            {'hour': f['hour'], 'temp': round(f['temp']), 'condition': f['condition']}
            for f in self.hourly_forecast[:3]
        ]

    def _get_daily_state(self) -> List[Dict[str, Any]]:
        """Get current daily forecast state for comparison."""
        if not self.daily_forecast:
            return None
        return [
            {
                'date': f['date'],
                'temp_high': round(f['temp_high']),
                'temp_low': round(f['temp_low']),
                'condition': f['condition']
            }
            for f in self.daily_forecast[:4]
        ]
    
    def _display_hourly_forecast(self) -> None:
        """Display hourly forecast with weather icons."""
        try:
            if not self.hourly_forecast:
                self._display_no_data()
                return
            
            # Check if state has changed
            current_state = self._get_hourly_state()
            if current_state == self.last_hourly_state:
                return
            
            # Clear the display
            self.display_manager.clear()
            
            # Create a new image for drawing
            img = Image.new('RGB', (self.display_manager.matrix.width, self.display_manager.matrix.height), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            # Calculate layout based on matrix dimensions
            hours_to_show = min(4, len(self.hourly_forecast))
            total_width = self.display_manager.matrix.width
            section_width = total_width // hours_to_show
            padding = max(2, section_width // 6)
            
            for i in range(hours_to_show):
                forecast = self.hourly_forecast[i]
                x = i * section_width + padding
                center_x = x + (section_width - 2 * padding) // 2
                
                # Draw hour at top
                hour_text = forecast['hour']
                hour_text = hour_text.replace(":00 ", "").replace("PM", "p").replace("AM", "a")
                hour_width = draw.textlength(hour_text, font=self.display_manager.small_font)
                draw.text((center_x - hour_width // 2, 1),
                         hour_text,
                         font=self.display_manager.small_font,
                         fill=self.COLORS['text'])
                
                # Draw weather icon centered vertically between top/bottom text
                icon_size = self.ICON_SIZE['large']
                top_text_height = 8
                bottom_text_y = self.display_manager.matrix.height - 8
                available_height_for_icon = bottom_text_y - top_text_height
                calculated_y = top_text_height + (available_height_for_icon - icon_size) // 2
                icon_y = (self.display_manager.matrix.height // 2) - 16
                icon_x = center_x - icon_size // 2
                WeatherIcons.draw_weather_icon(img, forecast['icon'], icon_x, icon_y, icon_size)
                
                # Draw temperature at bottom
                temp_text = f"{forecast['temp']}°"
                temp_width = draw.textlength(temp_text, font=self.display_manager.small_font)
                temp_y = self.display_manager.matrix.height - 8
                draw.text((center_x - temp_width // 2, temp_y),
                         temp_text,
                         font=self.display_manager.small_font,
                         fill=self.COLORS['text'])
            
            # Update the display
            self.display_manager.image = img
            self.display_manager.update_display()
            self.last_hourly_state = current_state

        except Exception as e:
            self.logger.error(f"Error displaying hourly forecast: {e}")
    
    def _display_daily_forecast(self) -> None:
        """Display daily forecast with weather icons."""
        try:
            if not self.daily_forecast:
                self._display_no_data()
                return
            
            # Check if state has changed
            current_state = self._get_daily_state()
            if current_state == self.last_daily_state:
                return
            
            # Clear the display
            self.display_manager.clear()
            
            # Create a new image for drawing
            img = Image.new('RGB', (self.display_manager.matrix.width, self.display_manager.matrix.height), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            # Calculate layout based on matrix dimensions for 3 days
            days_to_show = min(3, len(self.daily_forecast))
            if days_to_show == 0:
                # Handle case where there's no forecast data after filtering
                draw.text((2, 2), "No daily forecast", font=self.display_manager.small_font, fill=self.COLORS['dim'])
            else:
                total_width = self.display_manager.matrix.width
                section_width = total_width // days_to_show
                padding = max(2, section_width // 6)
                
                for i in range(days_to_show):
                    forecast = self.daily_forecast[i]
                    x = i * section_width
                    center_x = x + section_width // 2
                    
                    # Draw day name at top
                    day_text = forecast['date']
                    day_width = draw.textlength(day_text, font=self.display_manager.small_font)
                    draw.text((center_x - day_width // 2, 1),
                             day_text,
                             font=self.display_manager.small_font,
                             fill=self.COLORS['text'])
                    
                    # Draw weather icon centered vertically between top/bottom text
                    icon_size = self.ICON_SIZE['large']
                    top_text_height = 8
                    bottom_text_y = self.display_manager.matrix.height - 8
                    available_height_for_icon = bottom_text_y - top_text_height
                    calculated_y = top_text_height + (available_height_for_icon - icon_size) // 2
                    icon_y = (self.display_manager.matrix.height // 2) - 16
                    icon_x = center_x - icon_size // 2
                    WeatherIcons.draw_weather_icon(img, forecast['icon'], icon_x, icon_y, icon_size)
                    
                    # Draw high/low temperatures at bottom
                    temp_text = f"{forecast['temp_low']} / {forecast['temp_high']}"
                    temp_width = draw.textlength(temp_text, font=self.display_manager.extra_small_font)
                    temp_y = self.display_manager.matrix.height - 8
                    draw.text((center_x - temp_width // 2, temp_y),
                             temp_text,
                             font=self.display_manager.extra_small_font,
                             fill=self.COLORS['text'])
            
            # Update the display
            self.display_manager.image = img
            self.display_manager.update_display()
            self.last_daily_state = current_state

        except Exception as e:
            self.logger.error(f"Error displaying daily forecast: {e}")
    
    def get_info(self) -> Dict[str, Any]:
        """Return plugin info for web UI."""
        info = super().get_info()
        info.update({
            'location': self.location,
            'units': self.units,
            'api_key_configured': bool(self.api_key),
            'last_update': self.last_update,
            'current_temp': self.weather_data.get('main', {}).get('temp') if self.weather_data else None,
            'current_humidity': self.weather_data.get('main', {}).get('humidity') if self.weather_data else None,
            'current_description': self.weather_data.get('weather', [{}])[0].get('description', '') if self.weather_data else '',
            'forecast_available': bool(self.forecast_data),
            'daily_forecast_count': len(self.daily_forecast) if hasattr(self, 'daily_forecast') and self.daily_forecast is not None else 0,
            'hourly_forecast_count': len(self.hourly_forecast) if hasattr(self, 'hourly_forecast') and self.hourly_forecast is not None else 0
        })
        return info

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.weather_data = None
        self.forecast_data = None
        self.logger.info("Weather plugin cleaned up")

