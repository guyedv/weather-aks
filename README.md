# Weather API Project Overview

## About
The Weather API is a sophisticated data collection and analysis system that provides historical weather data for cities worldwide. It maintains a 30-day rolling window of weather information, automatically refreshing data daily while preventing duplicates through an intelligent fresh mechanism.

## Features

### Data Collection
- Collects daily weather data for user-requested cities
- Maintains a 30-day historical window of weather information
- Automatic daily data refresh mechanism
- Duplicate prevention system ensures data accuracy

### Data Storage
- First-time city requests trigger data collection and storage
- Subsequent requests utilize cached database data
- Azure Data Explorer (ADX) backend for efficient data storage and querying
- Intelligent data refresh mechanism updates stale records

### Weather Attributes
The system tracks various weather metrics including:
- Temperature (high/low)
- Humidity
- Wind speed and direction
- Precipitation
- Atmospheric pressure
- Weather conditions (sunny, cloudy, rain, etc.)

### Key Capabilities
1. **City-based Queries**: Users can request weather data for any city
2. **Historical Analysis**: Access to 30 days of historical weather data
3. **Fresh Data Mechanism**: 
   - Automatically refreshes data daily
   - Prevents duplicate entries
   - Updates existing records instead of creating duplicates
4. **Efficient Data Retrieval**:
   - First request: Collects and stores new data
   - Subsequent requests: Retrieves from database
   - Smart caching system for improved performance

## Technical Architecture

### Components
- Frontend API interface for user requests
- Azure Kubernetes Service (AKS) for deployment
- Azure Data Explorer (ADX) for data storage
- Azure Key Vault for secret management
- Ingress Controller for traffic management

### Data Flow
1. User submits city name
2. System checks if city data exists in database
3. If new city:
   - Collects 30 days of weather data
   - Stores in ADX database
4. If existing city:
   - Retrieves stored data
   - Checks freshness
   - Updates if necessary
5. Returns summarized weather attributes

### Security
- Secured endpoints
- Managed identities for Azure services
- Secret management through Azure Key Vault
- Role-based access control


```

## Maintenance and Updates
- Daily automated data refresh
- Regular system health checks
- Automatic scaling based on demand
- Monitoring and logging of all operations

## Future Enhancements
- Additional weather metrics
- Extended historical data range
- More detailed analysis capabilities
- Additional city information
- Weather prediction capabilities

For technical setup instructions, please refer to the Setup Guide README.
