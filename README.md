# Eco-Counter Data Scraper

A flexible Python scraper for extracting bicycle counter data from eco-counter websites for any city.

## Features

- Works with any eco-counter website (just provide the URL)
- Extracts counter metadata (ID, name, coordinates)
- Retrieves ADT (Average Daily Traffic) values for custom date ranges
- Extracts directional count data (separate ADT for each direction)
- Extracts installation dates and last data dates
- Outputs data in JSON and CSV formats with city-specific filenames
- Fully automated browser-based scraping using Playwright

## Installation

This project uses `uv` for dependency management.

```bash
# Install dependencies
uv sync

# Install Playwright browsers
uv run playwright install chromium
```

## Usage

### Basic Usage

```bash
# Scrape data for a city (default: May-September 2025)
uv run python scraper.py https://stadtaugsburg.eco-counter.com
```

This will generate:
- `stadtaugsburg_counters_data.json`
- `stadtaugsburg_counters_data.csv`

### Command-Line Arguments

```bash
# Show all available options
uv run python scraper.py --help

# Specify custom date range
uv run python scraper.py https://stadtaugsburg.eco-counter.com --start 2025-01-01 --end 2025-12-31

# Specify custom output filenames
uv run python scraper.py https://stadtaugsburg.eco-counter.com --json my_data.json --csv my_data.csv

# Scrape data for a different city
uv run python scraper.py https://paris.eco-counter.com --start 2025-05-01 --end 2025-09-30
```

**Arguments:**
- `url` (required): Base URL of the eco-counter site (e.g., `https://stadtaugsburg.eco-counter.com`)
- `--start`: Start date in YYYY-MM-DD format (default: `2025-05-01`)
- `--end`: End date in YYYY-MM-DD format (default: `2025-09-30`)
- `--json`: Output JSON filename (default: `{cityname}_counters_data.json`)
- `--csv`: Output CSV filename (default: `{cityname}_counters_data.csv`)

### Programmatic Usage

```python
from scraper import EcoCounterScraper

# Initialize scraper with URL and date range
scraper = EcoCounterScraper(
    base_url="https://stadtaugsburg.eco-counter.com",
    start_date="2025-05-01",
    end_date="2025-09-30"
)

# Run scraper
scraper.scrape()

# Save outputs
scraper.save_json()  # Saves to {cityname}_counters_data.json
scraper.save_csv()   # Saves to {cityname}_counters_data.csv

# Access raw data
for counter in scraper.counters_data:
    print(f"{counter['name']}: {counter['DZS_mean_SR']} bikes/day")
```

## Output Format

### CSV Structure

The CSV file contains **3 rows per counter** with directional data:

```csv
Counter_ID_orig,Counter_Name,lat,lon,DZS_Datenquelle,DZS_mean_SR,DZS_mean_year,year,DZS_installation_date,DZS_last_data_date,Richtung
100039435,Wagenhalsstraße,48.36483,10.90473,EcoCounter,1555,1352,2025,30.11.2017,20.1.2026,0
100039435,Wagenhalsstraße,48.36483,10.90473,EcoCounter,790,684,2025,30.11.2017,20.1.2026,1
100039435,Wagenhalsstraße,48.36483,10.90473,EcoCounter,764,667,2025,30.11.2017,20.1.2026,2
```

**Richtung column values:**
- `0` = Combined data (both directions)
- `1` = Direction 1 (e.g., "Ri. stadteinwärts (In)")
- `2` = Direction 2 (e.g., "Ri. stadtauswärts (Out)")

**Columns:**
- `Counter_ID_orig` (string): Original counter ID
- `Counter_Name` (string): Counter location name
- `lat` (float): Latitude (WGS84)
- `lon` (float): Longitude (WGS84)
- `DZS_Datenquelle` (string): Data source - always "EcoCounter"
- `DZS_mean_SR` (int): Daily average count for date range
- `DZS_mean_year` (int): Daily average count for full year 2025
- `year` (int): Year of the data - 2025
- `DZS_installation_date` (string): Installation date in DD.MM.YYYY format
- `DZS_last_data_date` (string): Last data date in DD.MM.YYYY format
- `Richtung` (int): Direction flag (0=both, 1=direction 1, 2=direction 2)

### JSON Format

```json
[
  {
    "id": "100039435",
    "name": "Wagenhalsstraße",
    "lat": 48.36483,
    "lon": 10.90473,
    "DZS_mean_SR": 1555,
    "DZS_mean_year": 1352,
    "DZS_installation_date": "30.11.2017",
    "DZS_last_data_date": "20.1.2026",
    "directions": [
      "Ri. stadteinwärts (In)",
      "Ri. stadtauswärts (Out)"
    ],
    "directional_counts_SR": [120939, 117005],
    "directional_counts_year": [249750, 243737],
    "year": 2025,
    "DZS_Datenquelle": "EcoCounter"
  }
]
```

## How It Works

### 1. Counter Metadata Extraction

The scraper navigates to the main eco-counter page and extracts counter metadata from embedded JavaScript:

```
https://{cityname}.eco-counter.com/?startDate=2025-05-01&endDate=2025-09-30
```

Counter data (ID, name, lat, lon) is embedded in Next.js server-rendered payload as escaped JSON.

### 2. ADT Value Extraction

For each counter, the scraper visits the individual counter page:

```
https://{cityname}.eco-counter.com/site/{COUNTER_ID}?startDate=2025-05-01&endDate=2025-09-30
```

The ADT value is extracted from a DOM element: `[data-testid="data-section-kpi-adt-value"]`

### 3. Directional Data Extraction

Directional counts are extracted from embedded JSON data in the page HTML. The scraper:
- Identifies direction labels (e.g., "Ri. stadteinwärts", "IN/OUT")
- Extracts monthly count totals for each direction
- Calculates average daily traffic (ADT) for each direction

### 4. Yearly Average Extraction

A second page request with full year dates extracts yearly averages:

```
https://{cityname}.eco-counter.com/site/{COUNTER_ID}?startDate=2025-01-01&endDate=2025-12-31
```

### 5. Data Processing

- Date formats converted from MM/DD/YYYY to DD.MM.YYYY
- Number formats converted to integers (removing thousands separators)
- UTF-8 encoding for international characters
- Error handling for missing data

## Performance

- **Metadata extraction:** ~3-5 seconds
- **ADT extraction:** ~4-5 seconds per counter (2 page loads)
- **Total time for 12 counters:** ~60-90 seconds

## Known Limitations

- No official API available (web scraping required)
- Depends on HTML structure (may break with website updates)
- Rate limiting unknown (delays recommended)
- Directional data may not be available for all counters

## Example: Stadt Augsburg

For Stadt Augsburg with 12 counters:

```bash
uv run python scraper.py https://stadtaugsburg.eco-counter.com --start 2025-05-01 --end 2025-09-30
```

Outputs:
- `stadtaugsburg_counters_data.csv` - 36 rows (12 counters × 3 directions)
- `stadtaugsburg_counters_data.json` - Full data with metadata

## Requirements

- Python 3.12+
- Playwright (with Chromium browser)
- uv (for dependency management)

## Project Structure

```
ecovisio-scraper/
├── scraper.py                        # Main scraper implementation
├── README.md                         # This file
├── pyproject.toml                    # Project dependencies
├── {cityname}_counters_data.json    # Output: JSON format
└── {cityname}_counters_data.csv     # Output: CSV format
```