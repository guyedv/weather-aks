import os
import io
from dotenv import load_dotenv, find_dotenv
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
KUSTO_QUERY_URI = "https://weather-cluster.israelcentral.kusto.windows.net"
KUSTO_INGEST_URI = "https://ingest-weather-cluster.israelcentral.kusto.windows.net"
KUSTO_DB = os.getenv("KUSTO_DB")
APP_ID = os.getenv("APP_ID")
APP_KEY = os.getenv("APP_KEY")
TENANT_ID = os.getenv("TENANT_ID")

# Initialize Kusto clients
try:
    # Query client
    query_kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
        KUSTO_QUERY_URI, APP_ID, APP_KEY, TENANT_ID
    )
    query_client = KustoClient(query_kcsb)

    # Ingestion client
    ingest_kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
        KUSTO_INGEST_URI, APP_ID, APP_KEY, TENANT_ID
    )
    ingest_client = KustoStreamingIngestClient(ingest_kcsb)
    print("✅ Successfully initialized Kusto clients")
except Exception as e:
    print(f"❌ Error initializing Kusto clients: {str(e)}")
    query_client = None
    ingest_client = None


async def get_city_coordinates(city: str):
    """Fetch latitude and longitude dynamically."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(GEO_API_URL, params={"name": city, "count": 1})
            geo_data = response.json()

        if "results" in geo_data and geo_data["results"]:
            return geo_data["results"][0]["latitude"], geo_data["results"][0]["longitude"], geo_data["results"][0]["name"]
        return None, None
    except Exception as e:
        print(f"❌ Error getting coordinates: {str(e)}")
        return None, None


def get_weather_stats(data):
    """Calculate weather statistics from daily data"""
    try:
        temps_max = data['temperature_2m_max']
        temps_min = data['temperature_2m_min']
        wind_max = data['wind_speed_10m_max']
        gusts_max = data['wind_gusts_10m_max']

        return {
            'current_temp': temps_max[-1],
            'current_wind': wind_max[-1],
            'min_temp': min(temps_min),
            'max_temp': max(temps_max),
            'avg_temp': round(sum(temps_max) / len(temps_max), 1),
            'min_wind': min(wind_max),
            'max_wind': max(gusts_max),
            'avg_wind': round(sum(wind_max) / len(wind_max), 1)
        }
    except Exception as e:
        print(f"❌ Error calculating weather stats: {str(e)}")
        return None


@app.get("/", response_class=HTMLResponse)
async def homepage():
    try:
        template = env.get_template("index.html")
        return template.render()
    except Exception as e:
        return HTMLResponse(f"<p>Error loading homepage: {str(e)}</p>")


async def get_weather_data(city: str, lat: float, lon: float):
    """Fetch weather data from Open-Meteo API"""
    try:
        async with httpx.AsyncClient() as client:
            weather_response = await client.get(WEATHER_API_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "sunrise",
                    "sunset",
                    "daylight_duration",
                    "sunshine_duration",
                    "uv_index_max",
                    "uv_index_clear_sky_max",
                    "precipitation_sum",
                    "rain_sum",
                    "showers_sum",
                    "snowfall_sum",
                    "precipitation_hours",
                    "precipitation_probability_max",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                    "wind_direction_10m_dominant",
                    "shortwave_radiation_sum",
                    "et0_fao_evapotranspiration"
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


@app.get("/weather", response_class=HTMLResponse)
async def get_weather(city: str = Query(..., description="Enter city name")):
    if not query_client or not ingest_client:
        return HTMLResponse("<p>Error: Kusto clients not properly initialized</p>")

    try:
        lat, lon, guessed_city = await get_city_coordinates(city)
        city = guessed_city.lower()

        # Step 1: Check if fresh data exists in ADX
        check_query = f"""
        WeatherData
        | where tolower(CityName) == '{city}'
        | where Date >= ago(30d)
        | summarize RecordCount = count()
        """
        response = query_client.execute(KUSTO_DB, check_query)
        result_count = response.primary_results[0][0]['RecordCount'] if response.primary_results[0] else 0

        # If we don't have 30 days of data, fetch and store new data
        if result_count < 30:
            # Fetch new data

            if lat is None or lon is None:
                return HTMLResponse("<p>Could not find coordinates for this city.</p>")

            weather_data = await get_weather_data(city, lat, lon)
            if not weather_data or "daily" not in weather_data:
                return HTMLResponse("<p>Error: Invalid response from weather API.</p>")

            daily_data = weather_data["daily"]
            ingestion_data, weather_attributes = prepare_ingestion_data(city, daily_data)

            if not ingestion_data:
                return HTMLResponse("<p>Error: Could not prepare weather data for storage.</p>")

            try:
                # Store data in ADX
                ingestion_props = IngestionProperties(
                    database=KUSTO_DB,
                    table="WeatherData",
                    data_format=DataFormat.CSV,
                    ingestion_mapping_kind=IngestionMappingKind.CSV,
                    ingestion_mapping_reference="WeatherDataMapping"
                )

                csv_data = "\n".join(ingestion_data)
                csv_stream = io.StringIO(csv_data)

                print("🔹 Ingesting Data into ADX...")
                ingest_client.ingest_from_stream(csv_stream, ingestion_properties=ingestion_props)
                print("✅ Data Ingestion Successful!")

                # Add a delay to allow for ingestion
                await asyncio.sleep(3)  # Wait for 10 seconds for data to be available

                # Verify data was stored
                verify_query = f"""
                WeatherData
                | where tolower(CityName) == '{city}'
                | where Date >= ago(30d)
                | summarize RecordCount = count()
                """
                verify_response = query_client.execute(KUSTO_DB, verify_query)
                verify_count = verify_response.primary_results[0][0]['RecordCount'] if verify_response.primary_results[
                    0] else 0

                if verify_count < 30:
                    return HTMLResponse(
                        "<p>Error: Data storage verification failed. Please try again in a few minutes.</p>")

            except Exception as e:
                print(f"❌ Error during data ingestion: {str(e)}")
                return HTMLResponse(f"<p>Error storing weather data: {str(e)}</p>")

        # At this point, we should have data in the database
        # Query the stored data to display
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
            CurrentWind = toscalar(WeatherData | where tolower(CityName) == '{city}' | top 1 by Date desc | project wind_speed_10m_max)

        """
        stats_response = query_client.execute(KUSTO_DB, stats_query)

        if not stats_response.primary_results[0]:
            return HTMLResponse("<p>Error: Could not retrieve weather data from database.</p>")

        row = stats_response.primary_results[0][0]
        stats = {
            'min_temp': row['MinTemp'],
            'max_temp': row['MaxTemp'],
            'avg_temp': round(row['AvgTemp'], 1),
            'min_wind': row['MinWind'],
            'max_wind': row['MaxWind'],
            'avg_wind': round(row['AvgWind'], 1),
            'current_max_temp': row['CurrentMaxTemp'],
            'current_min_temp': row['CurrentMinTemp'],
            'current_wind': row['CurrentWind']
        }

        return env.get_template("weather.html").render(
            city=city.capitalize(),
            **stats
        )

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return HTMLResponse(f"<p>An error occurred: {str(e)}</p>")