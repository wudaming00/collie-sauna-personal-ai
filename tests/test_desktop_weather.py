import json
import urllib.parse
import urllib.request

from harness import desktop


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_weather_shapes_current_hourly_and_daily_data(monkeypatch):
    geo = {"latitude": 37.3, "longitude": -121.9, "city": "San Jose",
           "region": "California", "country_code": "US", "timezone": "America/Los_Angeles"}
    hours = ["2026-08-24T%02d:00" % hour for hour in range(24)]
    forecast = {
        "timezone": "America/Los_Angeles",
        "current": {"time": "2026-08-24T15:00", "temperature_2m": 27.1,
                    "apparent_temperature": 28.2, "relative_humidity_2m": 42,
                    "precipitation": 0, "weather_code": 1, "is_day": 1,
                    "wind_speed_10m": 13.4, "wind_direction_10m": 305},
        "hourly": {"time": hours, "temperature_2m": list(range(24)),
                   "weather_code": [1] * 24, "precipitation_probability": [4] * 24,
                   "is_day": [1] * 19 + [0] * 5},
        "daily": {"time": ["2026-08-%02d" % day for day in range(24, 29)],
                  "weather_code": [1, 2, 3, 61, 0],
                  "temperature_2m_max": [31, 30, 28, 25, 29],
                  "temperature_2m_min": [17, 18, 16, 15, 17],
                  "precipitation_probability_max": [5, 10, 20, 60, 0],
                  "sunrise": ["2026-08-24T06:31"] * 5,
                  "sunset": ["2026-08-24T19:45"] * 5},
    }
    seen = []

    def fake_urlopen(request, timeout=0):
        seen.append(request.full_url)
        return _Response(geo if request.full_url.startswith("https://ipapi.co/") else forecast)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    desktop._WX_CACHE.update(at=0.0, data=None)
    result = desktop.weather()

    assert result["city"] == "San Jose" and result["feels_c"] == 28.2
    assert result["humidity"] == 42 and result["wind_kph"] == 13.4
    assert len(result["hourly"]) == 8 and result["hourly"][0]["at"] == "2026-08-24T15:00"
    assert result["hourly"][-1]["is_day"] == 0
    assert len(result["daily"]) == 5 and result["today"]["high_c"] == 31
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen[1]).query)
    assert "apparent_temperature" in query["current"][0]
    assert "precipitation_probability" in query["hourly"][0]
    assert query["forecast_days"] == ["5"] and query["timezone"] == ["auto"]
    desktop._WX_CACHE.update(at=0.0, data=None)
