import os
import io
from dotenv import load_dotenv, find_dotenv
from datetime import datetime  
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import httpx
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.ingest import IngestionProperties, QueuedIngestClient, KustoStreamingIngestClient
from azure.kusto.ingest.ingestion_properties import DataFormat, IngestionMappingKind
import asyncio

# Initialize FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
env = Environment(loader=FileSystemLoader("templates"))

# Load environment variables
load_dotenv(find_dotenv())

# API Endpoints
WEATHER_API_URL = "https://api.open-meteo.com/v1/forecast"
GEO_API_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Kusto configuration
KUSTO_CONFIG = {
    'query_uri': os.getenv("KUSTO_QUERY_URI"),
    'ingest_uri': os.getenv("KUSTO_INGEST_URI"),
    'db': os.getenv("KUSTO_DB"),
    'app_id': os.getenv("APP_ID"),
    'app_key': os.getenv("APP_KEY"),
    'tenant_id': os.getenv("TENANT_ID")
}

# Initialize Kusto clients
def initialize_kusto_clients():
    try:
        query_kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
            KUSTO_CONFIG['query_uri'],
            KUSTO_CONFIG['app_id'],
            KUSTO_CONFIG['app_key'],
            KUSTO_CONFIG['tenant_id']
        )
        query_client = KustoClient(query_kcsb)

        ingest_kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
            KUSTO_CONFIG['ingest_uri'],
            KUSTO_CONFIG['app_id'],
            KUSTO_CONFIG['app_key'],
            KUSTO_CONFIG['tenant_id']
        )
        ingest_client = KustoStreamingIngestClient(ingest_kcsb)
        print("✅ Successfully initialized Kusto clients")
        return query_client, ingest_client
    except Exception as e:
        print(f"❌ Error initializing Kusto clients: {str(e)}")
        return None, None

# Initialize clients at startup
query_client, ingest_client = initialize_kusto_clients()

@app.get("/", response_class=HTMLResponse)
async def homepage():
    try:
        template = env.get_template("index.html")
        return template.render()
    except Exception as e:
        return HTMLResponse(f"<p>Error loading homepage: {str(e)}</p>")


async def get_city_coordinates(city: str):
    """Fetch latitude and longitude and guess city name dynamically."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(GEO_API_URL, params={"name": city, "count": 1})
            geo_data = response.json()

        if "results" in geo_data and geo_data["results"]:
            result = geo_data["results"][0]
            return result["latitude"], result["longitude"], result["name"]
        return None, None, None
    except Exception as e:
        print(f"❌ Error getting coordinates: {str(e)}")
        return None, None, None


async def get_weather_data(lat: float, lon: float):
    """Fetch weather data from Open-Meteo API"""
    try:
        async with httpx.AsyncClient() as client:
            weather_response = await client.get(WEATHER_API_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": [
                    "weather_code", "temperature_2m_max", "temperature_2m_min",
                    "apparent_temperature_max", "apparent_temperature_min",
                    "sunrise", "sunset", "daylight_duration", "sunshine_duration",
                    "uv_index_max", "uv_index_clear_sky_max", "precipitation_sum",
                    "rain_sum", "showers_sum", "snowfall_sum", "precipitation_hours",
                    "precipitation_probability_max", "wind_speed_10m_max",
                    "wind_gusts_10m_max", "wind_direction_10m_dominant",
                    "shortwave_radiation_sum", "et0_fao_evapotranspiration"
                ],
                "timezone": "auto",
                "past_days": 30,
                "forecast_days": 1
            })
            return weather_response.json()
    except Exception as e:
        print(f"❌ Error fetching weather data: {str(e)}")
        return None


def prepare_ingestion_data(city: str, daily_data: dict):
    """Prepare weather data for ADX ingestion"""
    try:
        ingestion_data = []
        dates = daily_data["time"]
        # Get all keys except 'time' as they represent our weather attributes
        weather_attributes = [key for key in daily_data.keys() if key != "time"]

        for i in range(len(dates)):
            # Start with city and date
            row_values = [city, dates[i]]

            # Add all weather attributes in a consistent order
            for attr in weather_attributes:
                value = daily_data[attr][i]
                # Convert to string, handle None values
                row_values.append(str(value) if value is not None else "")

            # Join with commas
            row = ",".join(row_values)
            ingestion_data.append(row)

        return ingestion_data, weather_attributes
    except Exception as e:
        print(f"❌ Error preparing ingestion data: {str(e)}")
        return None, None

async def check_data_freshness(city: str) -> tuple[bool, str]:
    """
    Check if we have today's data for the city.
    Returns (is_fresh, latest_date) tuple.
    """
    latest_date_query = f"""
    WeatherData
    | where tolower(CityName) == '{city}'
    | summarize 
        LatestDate = max(Date),
        DataPoints = count()
    """
    response = query_client.execute(KUSTO_CONFIG['db'], latest_date_query)
    
    if not response.primary_results[0]:
        return False, ""
        
    row = response.primary_results[0][0]
    kusto_latest_date = row['LatestDate'] # e.g. "2025-02-17T00:00:00Z"
    data_points = row['DataPoints']

    # If no date is found, database does not contain city yet
    if kusto_latest_date is None:
        return False, ""

    latest_date = kusto_latest_date.strftime('%Y-%m-%d')
    latest_date_obj = datetime.strptime(latest_date, '%Y-%m-%d').date()
    today = datetime.now().date()
    date_difference = abs((today - latest_date_obj).days)
    
    is_fresh = date_difference <= 1 and data_points >= 30
    return is_fresh, latest_date

async def ensure_fresh_data(city: str, lat: float, lon: float) -> bool:
    """Ensure we have today's data and full 30-day history"""
    is_fresh, latest_date = await check_data_freshness(city)
    
    if not is_fresh:
        print(f"🔄 Updating data for {city}. Latest data was from {latest_date}")
        weather_data = await get_weather_data(lat, lon)
        if not weather_data or "daily" not in weather_data:
            return False

        daily_data = weather_data["daily"]
        
        if latest_date:
            try: 
                existing_dates_query = f"""
                WeatherData
                | where tolower(CityName) == '{city.lower()}'
                | project Date
                | extend DateStr = format_datetime(Date, 'yyyy-MM-dd')
                | summarize make_set(DateStr)
                """
                response = query_client.execute(KUSTO_CONFIG['db'], existing_dates_query)

                existing_dates = set()
                if response.primary_results[0]:
                    # Extract the set of dates from the response
                    existing_dates = set(response.primary_results[0][0]['set_DateStr'])
                
                print(f"Found existing dates in database: {existing_dates}")

                # Create a new filtered daily data dictionary
                filtered_daily_data = {
                    key: [] for key in daily_data.keys()
                }

                # Get the indices of dates we want to keep
                dates = daily_data["time"]
                for i in range(len(dates)):
                    if dates[i] not in existing_dates:
                        # For each key in daily_data, append the value at index i
                        for key in daily_data.keys():
                            filtered_daily_data[key].append(daily_data[key][i])

                print(f"Filtered out {len(dates) - len(filtered_daily_data['time'])} duplicate dates")
                
                # Only proceed with ingestion if we have new data
                if not filtered_daily_data['time']:
                    print("✅ No new dates to ingest")
                    return True
                
                daily_data = filtered_daily_data

            except Exception as e:
                print(f"❌ Error while fetching existing data: {str(e)}")
                return False

        ingestion_data, _ = prepare_ingestion_data(city, daily_data)
        if not ingestion_data:
            return False

        try:
            ingestion_props = IngestionProperties(
                database=KUSTO_CONFIG['db'],
                table="WeatherData",
                data_format=DataFormat.CSV,
                ingestion_mapping_kind=IngestionMappingKind.CSV,
                ingestion_mapping_reference="WeatherDataMapping"
            )

            csv_data = "\n".join(ingestion_data)
            csv_stream = io.StringIO(csv_data)

            print("🔹 Ingesting fresh data into ADX...")
            ingest_client.ingest_from_stream(
                csv_stream,
                ingestion_properties=ingestion_props
            )
            print("✅ Data Ingestion Successful!")
            
            # Verify the new data was stored
            is_fresh, new_latest_date = await check_data_freshness(city)
            if not is_fresh:
                print(f"❌ Data verification failed. Latest date after update: {new_latest_date}")
                return False
                
            return True

        except Exception as e:
            print(f"❌ Error during data ingestion: {str(e)}")
            return False
    
    print(f"✅ Data is fresh for {city}. Latest date: {latest_date}")
    return True
async def get_weather_stats(city: str):
    """Get weather statistics from the database"""
    stats_query = f"""
    WeatherData
    | where tolower(CityName) == '{city}'
    | where Date >= ago(30d)
    | summarize 
        MinTemp = min(temperature_2m_min), 
        MaxTemp = max(temperature_2m_max), 
        AvgTemp = avg((temperature_2m_max+temperature_2m_min)/2),
        MinWind = min(wind_speed_10m_max), 
        MaxWind = max(wind_speed_10m_max), 
        AvgWind = avg(wind_speed_10m_max),
        CurrentMaxTemp = toscalar(WeatherData | where tolower(CityName) == '{city}' | top 1 by Date desc | project temperature_2m_max),
        CurrentMinTemp = toscalar(WeatherData | where tolower(CityName) == '{city}' | top 1 by Date desc | project temperature_2m_min),
        CurrentWind = toscalar(WeatherData | where tolower(CityName) == '{city}' | top 1 by Date desc | project wind_speed_10m_max),
        CurrentWeatherCode = toscalar(WeatherData | where tolower(CityName) == '{city}' | top 1 by Date desc | project weather_code)
    """
    response = query_client.execute(KUSTO_CONFIG['db'], stats_query)
    
    if not response.primary_results[0]:
        return None

    row = response.primary_results[0][0]
    weather_codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Foggy",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow fall",
        73: "Moderate snow fall",
        75: "Heavy snow fall",
        77: "Snow grains",
        80: "Slight rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        85: "Slight snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail"
    }
    icon_map = {
        0: "01d",
        1: "01d",
        2: "02d",
        3: "03d",
        45: "03d",
        48: "03d",
        51: "09d",
        53: "09d",
        55: "09d",
        61: "09d",
        63: "09d",
        65: "09d",
        71: "13d",
        73: "13d",
        75: "13d",
        77: "13d",
        80: "09d",
        81: "09d",
        82: "09d",
        85: "13d",
        86: "13d",
        95: "11d",
        96: "11d",
        99: "11d"
     }
    weather_code = row['CurrentWeatherCode']
    weather_description = weather_codes.get(weather_code, "Unknown")
    icon_code = icon_map.get(weather_code, "01d")
    weather_icon_url = f"https://openweathermap.org/img/wn/{icon_code}@2x.png"

    return {
        'min_temp': row['MinTemp'],
        'max_temp': row['MaxTemp'],
        'avg_temp': round(row['AvgTemp'], 1),
        'min_wind': row['MinWind'],
        'max_wind': row['MaxWind'],
        'avg_wind': round(row['AvgWind'], 1),
        'current_max_temp': row['CurrentMaxTemp'],
        'current_min_temp': row['CurrentMinTemp'],
        'current_wind': row['CurrentWind'],
        'weather_description': weather_description,
        'weather_icon_url': weather_icon_url
    }

@app.get("/weather", response_class=HTMLResponse)
async def get_weather(city: str = Query(..., description="Enter city name")):
    """Handle weather requests"""
    if not query_client or not ingest_client:
        return HTMLResponse("<p>Error: Kusto clients not properly initialized</p>")

    try:
        # Get city coordinates
        lat, lon, guessed_city = await get_city_coordinates(city)
        if guessed_city is None:
            return HTMLResponse("<p>Error: Invalid city name, please try again.</p>")
        elif city.lower() != guessed_city.lower():
            return HTMLResponse(f"<p>Error: Inaccurate city name, Did you mean {guessed_city}? </p>")

        city = city.lower()

        # Ensure we have fresh data
        if not await ensure_fresh_data(city, lat, lon):
            return HTMLResponse("<p>Error: Could not ensure fresh weather data.</p>")

        # Get weather statistics
        stats = await get_weather_stats(city)
        if not stats:
            return HTMLResponse("<p>Error: Could not retrieve weather data from database.</p>")

        # Render template with data
        return env.get_template("weather.html").render(
            city=city.capitalize(),
            **stats
        )

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return HTMLResponse(f"<p>An error occurred: {str(e)}</p>")