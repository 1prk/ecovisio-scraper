"""
Eco-Counter Data Scraper for Stadt Augsburg

This script extracts bicycle counter data from the eco-counter website for any city if the URL format is in xxx.eco-counter.com -format..
It retrieves counter metadata (ID, name, coordinates) and daily average counts (ADT).

Supports two modes:
1. Web scraping mode: Scrapes data from xxx.eco-counter.com websites using Playwright
2. API mode: Fetches data directly from Eco-Visio API using organization ID

Output formats:
- JSON: Raw data output
- CSV: Formatted with columns: Counter_ID_orig, Counter_Name, lat, lon, DZS_mean_SR
"""

import json
import csv
import re
import sqlite3
import requests
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import quote
from pathlib import Path

# Note: playwright is imported lazily in EcoCounterScraper.scrape() only when needed


def init_database(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with schema for eco-counter data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY,
            name TEXT,
            country TEXT,
            logo_url TEXT,
            logo_data BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS counters (
            id INTEGER PRIMARY KEY,
            org_id INTEGER,
            name TEXT,
            lat REAL,
            lon REAL,
            install_date TEXT,
            last_data_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS counter_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            counter_id INTEGER,
            date_start TEXT,
            date_end TEXT,
            year INTEGER,
            adt_range INTEGER,
            adt_year INTEGER,
            direction INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (counter_id) REFERENCES counters(id)
        );

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            counter_id INTEGER,
            url TEXT,
            filename TEXT,
            content_type TEXT,
            image_data BLOB,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (counter_id) REFERENCES counters(id)
        );

        CREATE INDEX IF NOT EXISTS idx_counters_org ON counters(org_id);
        CREATE INDEX IF NOT EXISTS idx_counter_data_counter ON counter_data(counter_id);
        CREATE INDEX IF NOT EXISTS idx_images_counter ON images(counter_id);
    """)

    conn.commit()
    return conn


class EcoVisioAPI:
    """Fetches counter data directly from Eco-Visio API."""

    BASE_URL = "https://www.eco-visio.net/api/aladdin/1.0.0/pbl"

    def __init__(self, org_id: str, start_date: str, end_date: str, download_images: bool = False, images_dir: str = None):
        """
        Initialize API client.

        Args:
            org_id: Organization ID (e.g., "5417" for Augsburg)
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            download_images: Whether to download counter photos
            images_dir: Directory to save images (default: {org_name}_images)
        """
        self.org_id = org_id
        self.start_date = start_date
        self.end_date = end_date
        self.download_images = download_images
        self.images_dir = images_dir
        self.counters_data: List[Dict] = []
        self.org_name = None

    def _parse_date_api(self, date_str: str) -> Optional[datetime]:
        """Parse API date format (MM/DD/YYYY or DD/MM/YYYY) to datetime."""
        if not date_str:
            return None
        try:
            # Try MM/DD/YYYY first (API format)
            return datetime.strptime(date_str, '%m/%d/%Y')
        except ValueError:
            try:
                # Try DD/MM/YYYY
                return datetime.strptime(date_str, '%d/%m/%Y')
            except ValueError:
                return None

    def _format_date_german(self, dt: datetime) -> str:
        """Format datetime to German format (DD.MM.YYYY)."""
        return dt.strftime('%d.%m.%Y') if dt else ''

    def _date_to_api_format(self, date_str: str) -> str:
        """Convert YYYY-MM-DD to MM/DD/YYYY for API."""
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.strftime('%m/%d/%Y')

    def fetch_counters(self) -> List[Dict]:
        """
        Fetch all counters for the organization.

        Returns:
            List of counter metadata dictionaries
        """
        url = f"{self.BASE_URL}/publicwebpageplus/{self.org_id}"
        print(f"Fetching counters from: {url}")

        response = requests.get(url, timeout=30)
        response.raise_for_status()
        counters = response.json()

        if counters and len(counters) > 0:
            self.org_name = counters[0].get('nomOrganisme', f'org_{self.org_id}')

        print(f"Found {len(counters)} counters for {self.org_name}")
        return counters

    def fetch_counter_details(self, counter_id: int) -> Dict:
        """
        Fetch detailed counter info from publicwebpage endpoint.

        Returns installation date and channel info for directional data.
        """
        url = f"{self.BASE_URL}/publicwebpage/{counter_id}"
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return {}

    def fetch_daily_data(self, counter_id: int, flow_ids: List[int]) -> List[tuple]:
        """
        Fetch daily count data for a counter.

        Args:
            counter_id: Counter ID (idPdc)
            flow_ids: List of flow IDs to fetch

        Returns:
            List of (date, count) tuples
        """
        if not flow_ids:
            return []

        # Join flow IDs with semicolons (URL-encoded)
        flow_ids_str = quote(';'.join(str(fid) for fid in flow_ids))
        url = f"{self.BASE_URL}/publicwebpageplus/data/{counter_id}?idOrganisme={self.org_id}&idPdc={counter_id}&interval=4&flowIds={flow_ids_str}"

        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            data = response.json()

            # Parse the data: [["MM/DD/YYYY", "count"], ...]
            result = []
            for entry in data:
                if len(entry) >= 2:
                    date_str = entry[0]
                    count = int(entry[1]) if entry[1] else 0
                    dt = self._parse_date_api(date_str)
                    if dt:
                        result.append((dt, count))
            return result
        except requests.RequestException as e:
            print(f"    Error fetching data: {e}")
            return []

    def calculate_adt(self, daily_data: List[tuple], start_date: datetime, end_date: datetime) -> Optional[int]:
        """
        Calculate average daily traffic for a date range.

        Args:
            daily_data: List of (datetime, count) tuples
            start_date: Start of range
            end_date: End of range

        Returns:
            Average daily count rounded to integer, or None if no data
        """
        # Filter data to the date range
        filtered = [(dt, count) for dt, count in daily_data if start_date <= dt <= end_date]

        if not filtered:
            return None

        total = sum(count for _, count in filtered)
        days = len(filtered)
        return round(total / days) if days > 0 else None

    def scrape(self):
        """Main method to fetch all counter data."""
        print("=" * 60)
        print(f"Eco-Visio API Client")
        print("=" * 60)
        print(f"Organization ID: {self.org_id}")
        print(f"Date range: {self.start_date} to {self.end_date}")
        print()

        # Parse date range for filtering
        start_dt = datetime.strptime(self.start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(self.end_date, '%Y-%m-%d')

        # Date range for full year calculation
        year = int(self.start_date.split('-')[0])
        year_start = datetime(year, 1, 1)
        year_end = datetime(year, 12, 31)

        # Step 1: Fetch counter list
        print("Step 1: Fetching counter list...")
        counters = self.fetch_counters()

        # Step 2: Fetch detailed data for each counter
        print("\nStep 2: Fetching detailed counter data...")
        for counter in counters:
            counter_id = counter['idPdc']
            counter_name = counter['nom']
            print(f"\n  Processing: {counter_name} (ID: {counter_id})")

            # Get detailed info including channels
            details = self.fetch_counter_details(counter_id)

            # Get installation and last data dates
            install_date = None
            if details.get('date'):
                # Format: YYYY-MM-DD
                try:
                    install_date = datetime.strptime(details['date'], '%Y-%m-%d')
                except ValueError:
                    pass

            # Parse debut from counter metadata (first data date)
            debut = counter.get('debut')
            if debut and not install_date:
                install_date = self._parse_date_api(debut)

            # Last data date from 'today' field
            last_data = self._parse_date_api(counter.get('today'))

            # Get flow IDs from pratique array
            pratique = counter.get('pratique', [])
            all_flow_ids = [p['id'] for p in pratique if 'id' in p]

            # Get directional channels if available
            channels = details.get('channels', [])
            in_channels = [c['id'] for c in channels if c.get('sens') == 1]
            out_channels = [c['id'] for c in channels if c.get('sens') == 2]

            # Fetch combined data (all flow IDs)
            print(f"    Fetching daily data...")
            combined_data = self.fetch_daily_data(counter_id, all_flow_ids)

            # Calculate ADT for date range
            adt_sr = self.calculate_adt(combined_data, start_dt, end_dt)
            if adt_sr is not None:
                print(f"    ADT (range): {adt_sr}")

            # Calculate ADT for full year
            adt_year = self.calculate_adt(combined_data, year_start, year_end)
            if adt_year is not None:
                print(f"    ADT (year {year}): {adt_year}")

            # Fetch directional data if channels exist
            dir1_adt_sr = None
            dir1_adt_year = None
            dir2_adt_sr = None
            dir2_adt_year = None

            if in_channels:
                in_data = self.fetch_daily_data(counter_id, in_channels)
                dir1_adt_sr = self.calculate_adt(in_data, start_dt, end_dt)
                dir1_adt_year = self.calculate_adt(in_data, year_start, year_end)
                if dir1_adt_sr is not None:
                    print(f"    Direction 1 ADT (range): {dir1_adt_sr}")

            if out_channels:
                out_data = self.fetch_daily_data(counter_id, out_channels)
                dir2_adt_sr = self.calculate_adt(out_data, start_dt, end_dt)
                dir2_adt_year = self.calculate_adt(out_data, year_start, year_end)
                if dir2_adt_sr is not None:
                    print(f"    Direction 2 ADT (range): {dir2_adt_sr}")

            # Get photo URLs from counter metadata
            photos = counter.get('photo', [])
            photo_urls = [p.get('lien') for p in photos if p.get('lien')]

            # Build counter data record
            counter_record = {
                'id': str(counter_id),
                'name': counter_name,
                'lat': counter['lat'],
                'lon': counter['lon'],
                'DZS_Datenquelle': 'EcoCounter',
                'DZS_mean_SR': adt_sr,
                'DZS_mean_year': adt_year,
                'year': year,
                'DZS_installation_date': self._format_date_german(install_date) if install_date else '',
                'DZS_last_data_date': self._format_date_german(last_data) if last_data else '',
                'directions': [],
                'directional_adt_sr': [],
                'directional_adt_year': [],
                'photo_urls': photo_urls,
            }

            # Add directional data
            if in_channels or out_channels:
                counter_record['directions'] = ['IN', 'OUT']
                if dir1_adt_sr is not None:
                    counter_record['directional_adt_sr'].append(dir1_adt_sr)
                if dir2_adt_sr is not None:
                    counter_record['directional_adt_sr'].append(dir2_adt_sr)
                if dir1_adt_year is not None:
                    counter_record['directional_adt_year'].append(dir1_adt_year)
                if dir2_adt_year is not None:
                    counter_record['directional_adt_year'].append(dir2_adt_year)

            self.counters_data.append(counter_record)

        print("\n" + "=" * 60)
        print(f"Data collection completed! Processed {len(self.counters_data)} counters")
        print("=" * 60)

    def save_json(self, filename: str = None):
        """Save data to JSON file."""
        if filename is None:
            # Clean org name for filename
            safe_name = re.sub(r'[^\w\s-]', '', self.org_name or f'org_{self.org_id}')
            safe_name = re.sub(r'\s+', '_', safe_name).lower()
            filename = f"{safe_name}_counters_data.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.counters_data, f, ensure_ascii=False, indent=2)
        print(f"\nJSON data saved to: {filename}")

    def save_csv(self, filename: str = None):
        """
        Save data to CSV file with specified column names.

        Creates multiple rows per counter:
        - Row with Richtung=0: Combined data (both directions)
        - Row with Richtung=1: Direction 1 data (if directional)
        - Row with Richtung=2: Direction 2 data (if directional)
        """
        if filename is None:
            safe_name = re.sub(r'[^\w\s-]', '', self.org_name or f'org_{self.org_id}')
            safe_name = re.sub(r'\s+', '_', safe_name).lower()
            filename = f"{safe_name}_counters_data.csv"

        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'Counter_ID_orig', 'Counter_Name', 'lat', 'lon',
                'DZS_Datenquelle', 'DZS_mean_SR', 'DZS_mean_year', 'year',
                'DZS_installation_date', 'DZS_last_data_date', 'Richtung'
            ])
            writer.writeheader()

            for counter in self.counters_data:
                # Common fields for all rows
                base_row = {
                    'Counter_ID_orig': counter['id'],
                    'Counter_Name': counter['name'],
                    'lat': counter['lat'],
                    'lon': counter['lon'],
                    'DZS_Datenquelle': counter.get('DZS_Datenquelle', 'EcoCounter'),
                    'year': counter.get('year', ''),
                    'DZS_installation_date': counter.get('DZS_installation_date', ''),
                    'DZS_last_data_date': counter.get('DZS_last_data_date', ''),
                }

                # Row 1: Richtung=0 (combined/total)
                row_total = base_row.copy()
                row_total['DZS_mean_SR'] = counter.get('DZS_mean_SR', '')
                row_total['DZS_mean_year'] = counter.get('DZS_mean_year', '')
                row_total['Richtung'] = 0
                writer.writerow(row_total)

                # Add directional rows if available
                dir_adt_sr = counter.get('directional_adt_sr', [])
                dir_adt_year = counter.get('directional_adt_year', [])

                if len(dir_adt_sr) >= 1:
                    row_dir1 = base_row.copy()
                    row_dir1['DZS_mean_SR'] = dir_adt_sr[0]
                    row_dir1['DZS_mean_year'] = dir_adt_year[0] if len(dir_adt_year) >= 1 else ''
                    row_dir1['Richtung'] = 1
                    writer.writerow(row_dir1)

                if len(dir_adt_sr) >= 2:
                    row_dir2 = base_row.copy()
                    row_dir2['DZS_mean_SR'] = dir_adt_sr[1]
                    row_dir2['DZS_mean_year'] = dir_adt_year[1] if len(dir_adt_year) >= 2 else ''
                    row_dir2['Richtung'] = 2
                    writer.writerow(row_dir2)

        print(f"CSV data saved to: {filename}")

    def save_sqlite(self, filename: str = None, download_images: bool = True):
        """
        Save data to SQLite database with images as BLOBs.

        Args:
            filename: Database filename (default: {org_name}_data.db)
            download_images: Whether to download and store counter photos
        """
        if filename is None:
            safe_name = re.sub(r'[^\w\s-]', '', self.org_name or f'org_{self.org_id}')
            safe_name = re.sub(r'\s+', '_', safe_name).lower()
            filename = f"{safe_name}_data.db"

        print(f"\nSaving to SQLite: {filename}")

        conn = init_database(filename)
        cursor = conn.cursor()

        # Insert organization
        cursor.execute("""
            INSERT OR REPLACE INTO organizations (id, name, country)
            VALUES (?, ?, ?)
        """, (int(self.org_id), self.org_name, 'de'))  # TODO: get country from API

        # Insert counters and their data
        for counter in self.counters_data:
            counter_id = int(counter['id'])

            # Insert counter
            cursor.execute("""
                INSERT OR REPLACE INTO counters (id, org_id, name, lat, lon, install_date, last_data_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                counter_id,
                int(self.org_id),
                counter['name'],
                counter['lat'],
                counter['lon'],
                counter.get('DZS_installation_date', ''),
                counter.get('DZS_last_data_date', ''),
            ))

            # Insert counter data (combined - direction 0)
            cursor.execute("""
                INSERT INTO counter_data (counter_id, date_start, date_end, year, adt_range, adt_year, direction)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                counter_id,
                self.start_date,
                self.end_date,
                counter.get('year'),
                counter.get('DZS_mean_SR'),
                counter.get('DZS_mean_year'),
                0,
            ))

            # Insert directional data
            dir_adt_sr = counter.get('directional_adt_sr', [])
            dir_adt_year = counter.get('directional_adt_year', [])

            for i, (adt_sr, adt_year) in enumerate(zip(dir_adt_sr, dir_adt_year), 1):
                cursor.execute("""
                    INSERT INTO counter_data (counter_id, date_start, date_end, year, adt_range, adt_year, direction)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    counter_id,
                    self.start_date,
                    self.end_date,
                    counter.get('year'),
                    adt_sr,
                    adt_year,
                    i,
                ))

            # Download and store images
            if download_images:
                photo_urls = counter.get('photo_urls', [])
                for idx, url in enumerate(photo_urls):
                    try:
                        # Skip if image already exists
                        cursor.execute("SELECT 1 FROM images WHERE counter_id = ? AND url = ?", (counter_id, url))
                        if cursor.fetchone():
                            print(f"    Image already exists, skipping: {counter['name'][:30]}")
                            continue

                        print(f"    Downloading image {idx + 1}/{len(photo_urls)} for {counter['name'][:30]}...")
                        response = requests.get(url, timeout=30)
                        response.raise_for_status()

                        # Get filename from URL
                        url_filename = Path(url).name or f"image_{idx}.jpg"
                        content_type = response.headers.get('Content-Type', 'image/jpeg')

                        cursor.execute("""
                            INSERT INTO images (counter_id, url, filename, content_type, image_data)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            counter_id,
                            url,
                            url_filename,
                            content_type,
                            response.content,
                        ))
                    except requests.RequestException as e:
                        print(f"    Error downloading image: {e}")

        conn.commit()
        conn.close()

        print(f"SQLite database saved to: {filename}")

        # Print summary
        conn = sqlite3.connect(filename)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM counters")
        counter_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM images")
        image_count = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(LENGTH(image_data)) FROM images")
        total_size = cursor.fetchone()[0] or 0
        conn.close()

        print(f"  Counters: {counter_count}")
        print(f"  Images: {image_count} ({total_size / 1024 / 1024:.1f} MB)")


class EcoCounterScraper:
    """Scraper for eco-counter website data."""

    def __init__(self, base_url: str, start_date: str, end_date: str):
        """
        Initialize scraper with base URL and date range.

        Args:
            base_url: Base URL of eco-counter site (e.g., "https://stadtaugsburg.eco-counter.com")
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        self.base_url = base_url.rstrip('/')
        self.start_date = start_date
        self.end_date = end_date
        self.counters_data: List[Dict] = []

        # Extract city name from URL
        # URL format: https://{cityname}.eco-counter.com
        import re
        match = re.search(r'https?://([^.]+)\.eco-counter\.com', base_url)
        if match:
            self.city_name = match.group(1)
        else:
            # Fallback: use domain as city name
            self.city_name = base_url.replace('https://', '').replace('http://', '').split('.')[0]

    def extract_counter_metadata(self, page) -> List[Dict]:
        """
        Extract counter metadata (ID, name, lat, lon) from main page.

        The metadata is embedded in the page's JavaScript as escaped JSON.
        We search for the pattern: \"id\":number,\"name\":\"...\",\"location\":{\"lat\":...,\"lon\":...}

        Args:
            page: Playwright page object

        Returns:
            List of dictionaries with keys: id, name, lat, lon
        """
        url = f"{self.base_url}/?startDate={self.start_date}&endDate={self.end_date}"
        print(f"Navigating to main page: {url}")
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # Extract site data from embedded JavaScript
        sites = page.evaluate("""
            () => {
                const scripts = document.querySelectorAll('script');
                let content = '';

                // Find script containing site data (look for pattern indicating site data)
                for (let i = 0; i < scripts.length; i++) {
                    const text = scripts[i].textContent || '';
                    // Look for sites data structure pattern
                    if (text.indexOf('"sites":') > -1 || text.indexOf('\\\\"location\\\\":') > -1) {
                        content = text;
                        break;
                    }
                }

                const sites = [];
                // Search for escaped JSON pattern: \\"id\\":
                const searchStr = String.fromCharCode(92, 34, 105, 100, 92, 34, 58);
                let pos = 0;

                while (true) {
                    const idxFound = content.indexOf(searchStr, pos);
                    if (idxFound === -1) break;

                    const idStart = idxFound + searchStr.length;
                    const idEnd = content.indexOf(',', idStart);
                    const id = content.substring(idStart, idEnd).trim();

                    // Check if it's a site ID (common eco-counter patterns: 100..., 300..., or other 8+ digit IDs)
                    if (id.length >= 8 && /^[13]\\d{8,}/.test(id)) {
                        const chunk = content.substring(idxFound, idxFound + 600);

                        // Extract name: \\"name\\":\\"...
                        const namePattern = String.fromCharCode(92, 34, 110, 97, 109, 101, 92, 34, 58, 92, 34);
                        const nameIdx = chunk.indexOf(namePattern);
                        if (nameIdx > -1) {
                            const nameStart = nameIdx + namePattern.length;
                            const nameEnd = chunk.indexOf(String.fromCharCode(92, 34), nameStart);
                            const name = chunk.substring(nameStart, nameEnd);

                            // Extract lat: \\"lat\\":
                            const latPattern = String.fromCharCode(92, 34, 108, 97, 116, 92, 34, 58);
                            const latIdx = chunk.indexOf(latPattern);
                            if (latIdx > -1) {
                                const latStart = latIdx + latPattern.length;
                                const lat = parseFloat(chunk.substring(latStart, chunk.indexOf(',', latStart)));

                                // Extract lon: \\"lon\\":
                                const lonPattern = String.fromCharCode(92, 34, 108, 111, 110, 92, 34, 58);
                                const lonIdx = chunk.indexOf(lonPattern);
                                if (lonIdx > -1) {
                                    const lonStart = lonIdx + lonPattern.length;
                                    const lon = parseFloat(chunk.substring(lonStart, chunk.indexOf('}', lonStart)));

                                    if (!isNaN(lat) && !isNaN(lon)) {
                                        sites.push({id: id, name: name, lat: lat, lon: lon});
                                    }
                                }
                            }
                        }
                    }

                    pos = idxFound + 1;
                    if (sites.length > 1000) break;
                }

                return sites;
            }
        """)

        print(f"Found {len(sites)} counters")
        return sites

    def extract_counter_details(self, page, site_id: str) -> Dict:
        """
        Extract detailed counter data from individual counter page.

        Extracts:
        - ADT for specified date range (DZS_mean_SR)
        - Yearly ADT for full year 2025 (DZS_mean_year)
        - Installation date (DZS_installation_date)
        - Last data date (DZS_last_data_date)
        - Directional data if available

        Args:
            page: Playwright page object
            site_id: Counter site ID

        Returns:
            Dictionary with extracted values
        """
        print(f"  Fetching data for site {site_id}...")

        result = {
            'DZS_mean_SR': None,
            'DZS_mean_year': None,
            'DZS_installation_date': None,
            'DZS_last_data_date': None,
            'directions': [],
            'directional_counts_SR': [],  # List of counts for each direction (date range)
            'directional_counts_year': []  # List of counts for each direction (full year)
        }

        try:
            # First, get ADT for the specified date range
            url = f"{self.base_url}/site/{site_id}?startDate={self.start_date}&endDate={self.end_date}"
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(3000)  # Increased wait time for full page load

            # Extract ADT value
            adt_element = page.query_selector('[data-testid="data-section-kpi-adt-value"]')
            if adt_element:
                adt_text = adt_element.text_content().strip()
                # Playwright returns comma as thousands separator (e.g., "3,053")
                # Remove thousands separators and parse as integer
                cleaned_text = adt_text.replace(',', '').replace('.', '')
                result['DZS_mean_SR'] = int(cleaned_text) if cleaned_text.isdigit() else None
                print(f"    ADT (range): {result['DZS_mean_SR']}")

            # Extract installation and last data dates using JavaScript
            # Dates in HTML use slash format (MM/DD/YYYY), convert to German format (DD.MM.YYYY)
            dates = page.evaluate("""
                () => {
                    const fullHtml = document.documentElement.innerHTML;

                    // Helper function to convert MM/DD/YYYY to DD.MM.YYYY
                    const convertDate = (dateStr) => {
                        const parts = dateStr.split('/');
                        if (parts.length === 3) {
                            return `${parts[1]}.${parts[0]}.${parts[2]}`;
                        }
                        return dateStr;
                    };

                    // Extract installation date (with slashes)
                    const installMatch = fullHtml.match(/Installation.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                    const installDate = installMatch ? convertDate(installMatch[1]) : null;

                    // Extract last data date (with slashes)
                    // Try multiple patterns for "Last data"
                    let lastMatch = fullHtml.match(/Letzte Daten.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                    if (!lastMatch) {
                        // Try looking for the data-testid attribute
                        lastMatch = fullHtml.match(/last-data-date.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                    }
                    const lastDate = lastMatch ? convertDate(lastMatch[1]) : null;

                    return {
                        installation: installDate,
                        lastData: lastDate
                    };
                }
            """)

            if dates and dates.get('installation'):
                result['DZS_installation_date'] = dates['installation']
                print(f"    Installation: {result['DZS_installation_date']}")

            if dates and dates.get('lastData'):
                result['DZS_last_data_date'] = dates['lastData']
                print(f"    Last data: {result['DZS_last_data_date']}")

            # Extract directional data from "Richtungsbezogene Zähldaten" section
            direction_data = page.evaluate("""
                () => {
                    const directions = new Set();
                    // Look for direction labels (Ri. or IN/OUT patterns)
                    const allText = document.body.textContent;

                    // Pattern 1: "Ri. Name (In/Out)"
                    const riPattern = /Ri\\. ([^(]+) \\((In|Out)\\)/g;
                    let match;
                    while ((match = riPattern.exec(allText)) !== null) {
                        directions.add('Ri. ' + match[1].trim() + ' (' + match[2] + ')');
                    }

                    // Pattern 2: "IN (In)" or "OUT (Out)"
                    if (allText.includes('IN (In)') && allText.includes('OUT (Out)')) {
                        directions.add('IN');
                        directions.add('OUT');
                    }

                    const directionNames = Array.from(directions).slice(0, 2);

                    // Extract directional counts from embedded JSON
                    const bodyHtml = document.body.innerHTML;
                    const directionalCounts = [];

                    const inIndex = bodyHtml.indexOf('\\\\"direction\\\\":\\\\"in\\\\"');
                    const outIndex = bodyHtml.indexOf('\\\\"direction\\\\":\\\\"out\\\\"');

                    if (inIndex > -1) {
                        const inChunk = bodyHtml.substring(inIndex, inIndex + 3000);
                        const inCountsMatch = inChunk.match(/\\\\"data\\\\":\\[([^\\]]+)\\]/);
                        let inTotalCounts = 0;

                        if (inCountsMatch) {
                            const countsPattern = /\\\\"counts\\\\":(\\d+)/g;
                            let match;
                            while ((match = countsPattern.exec(inCountsMatch[1])) !== null) {
                                inTotalCounts += parseInt(match[1]);
                            }
                        }

                        directionalCounts.push(inTotalCounts);
                    }

                    if (outIndex > -1) {
                        const outChunk = bodyHtml.substring(outIndex, outIndex + 3000);
                        const outCountsMatch = outChunk.match(/\\\\"data\\\\":\\[([^\\]]+)\\]/);
                        let outTotalCounts = 0;

                        if (outCountsMatch) {
                            const countsPattern = /\\\\"counts\\\\":(\\d+)/g;
                            let match;
                            while ((match = countsPattern.exec(outCountsMatch[1])) !== null) {
                                outTotalCounts += parseInt(match[1]);
                            }
                        }

                        directionalCounts.push(outTotalCounts);
                    }

                    return {
                        names: directionNames,
                        counts: directionalCounts
                    };
                }
            """)

            result['directions'] = direction_data['names']
            if direction_data['counts']:
                result['directional_counts_SR'] = direction_data['counts']

            if result['directions']:
                print(f"    Directions: {', '.join(result['directions'])}")
                if result['directional_counts_SR']:
                    # Calculate total days in date range
                    from datetime import datetime
                    start = datetime.strptime(self.start_date, '%Y-%m-%d')
                    end = datetime.strptime(self.end_date, '%Y-%m-%d')
                    total_days = (end - start).days + 1

                    dir_adts = [count / total_days for count in result['directional_counts_SR']]
                    print(f"    Directional ADTs: {[f'{adt:.1f}' for adt in dir_adts]}")

        except Exception as e:
            print(f"    Error extracting data: {e}")

        # Now get yearly average (full year 2025)
        try:
            year_url = f"{self.base_url}/site/{site_id}?startDate=2025-01-01&endDate=2025-12-31"
            page.goto(year_url, wait_until="networkidle")
            page.wait_for_timeout(2000)

            adt_year_element = page.query_selector('[data-testid="data-section-kpi-adt-value"]')
            if adt_year_element:
                adt_year_text = adt_year_element.text_content().strip()
                # Remove thousands separators and parse as integer
                cleaned_year_text = adt_year_text.replace(',', '').replace('.', '')
                result['DZS_mean_year'] = int(cleaned_year_text) if cleaned_year_text.isdigit() else None
                print(f"    ADT (year 2025): {result['DZS_mean_year']}")

            # Extract directional counts for full year if this counter has directions
            if result['directions']:
                year_direction_counts = page.evaluate("""
                    () => {
                        const bodyHtml = document.body.innerHTML;
                        const directionalCounts = [];

                        const inIndex = bodyHtml.indexOf('\\\\"direction\\\\":\\\\"in\\\\"');
                        const outIndex = bodyHtml.indexOf('\\\\"direction\\\\":\\\\"out\\\\"');

                        if (inIndex > -1) {
                            const inChunk = bodyHtml.substring(inIndex, inIndex + 10000);
                            const inCountsMatch = inChunk.match(/\\\\"data\\\\":\\[([^\\]]+)\\]/);
                            let inTotalCounts = 0;

                            if (inCountsMatch) {
                                const countsPattern = /\\\\"counts\\\\":(\\d+)/g;
                                let match;
                                while ((match = countsPattern.exec(inCountsMatch[1])) !== null) {
                                    inTotalCounts += parseInt(match[1]);
                                }
                            }

                            directionalCounts.push(inTotalCounts);
                        }

                        if (outIndex > -1) {
                            const outChunk = bodyHtml.substring(outIndex, outIndex + 10000);
                            const outCountsMatch = outChunk.match(/\\\\"data\\\\":\\[([^\\]]+)\\]/);
                            let outTotalCounts = 0;

                            if (outCountsMatch) {
                                const countsPattern = /\\\\"counts\\\\":(\\d+)/g;
                                let match;
                                while ((match = countsPattern.exec(outCountsMatch[1])) !== null) {
                                    outTotalCounts += parseInt(match[1]);
                                }
                            }

                            directionalCounts.push(outTotalCounts);
                        }

                        return directionalCounts;
                    }
                """)

                if year_direction_counts:
                    result['directional_counts_year'] = year_direction_counts
                    year_dir_adts = [count / 365 for count in year_direction_counts]
                    print(f"    Directional ADTs (year): {[f'{adt:.1f}' for adt in year_dir_adts]}")

        except Exception as e:
            print(f"    Error extracting yearly ADT: {e}")

        return result

    def scrape(self):
        """Main scraping method."""
        # Import playwright only when needed (web scraping mode)
        from playwright.sync_api import sync_playwright

        print("=" * 60)
        print(f"Eco-Counter Scraper for {self.city_name}")
        print("=" * 60)
        print(f"URL: {self.base_url}")
        print(f"Date range: {self.start_date} to {self.end_date}")
        print()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            try:
                # Step 1: Extract counter metadata
                print("Step 1: Extracting counter metadata...")
                counters = self.extract_counter_metadata(page)

                # Step 2: Extract detailed data for each counter
                print("\nStep 2: Extracting detailed counter data...")
                for counter in counters:
                    details = self.extract_counter_details(page, counter['id'])
                    counter.update(details)

                    # Extract year from start_date for the year column
                    counter['year'] = int(self.start_date.split('-')[0])

                    # Add data source
                    counter['DZS_Datenquelle'] = 'EcoCounter'

                self.counters_data = counters

            finally:
                browser.close()

        print("\n" + "=" * 60)
        print(f"Scraping completed! Extracted data for {len(self.counters_data)} counters")
        print("=" * 60)

    def save_json(self, filename: str = None):
        """
        Save data to JSON file.

        Args:
            filename: Output filename (default: {cityname}_counters_data.json)
        """
        if filename is None:
            filename = f"{self.city_name}_counters_data.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.counters_data, f, ensure_ascii=False, indent=2)
        print(f"\nJSON data saved to: {filename}")

    def save_csv(self, filename: str = None):
        """
        Save data to CSV file with specified column names.

        Creates multiple rows per counter:
        - Row with Richtung=0: Combined data (both directions)
        - Row with Richtung=1: Direction 1 data (if directional)
        - Row with Richtung=2: Direction 2 data (if directional)

        Columns:
        - Counter_ID_orig: Original counter ID (string)
        - Counter_Name: Counter name (string)
        - lat: Latitude (float)
        - lon: Longitude (float)
        - DZS_Datenquelle: Data source (string) - "EcoCounter"
        - DZS_mean_SR: Daily average for date range (int)
        - DZS_mean_year: Daily average for full year 2025 (int)
        - year: Year of the data (int) - 2025
        - DZS_installation_date: Installation date (string)
        - DZS_last_data_date: Last data date (string)
        - Richtung: Direction flag (0=both, 1=direction 1, 2=direction 2)

        Args:
            filename: Output filename (default: {cityname}_counters_data.csv)
        """
        if filename is None:
            filename = f"{self.city_name}_counters_data.csv"
        from datetime import datetime

        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'Counter_ID_orig', 'Counter_Name', 'lat', 'lon',
                'DZS_Datenquelle', 'DZS_mean_SR', 'DZS_mean_year', 'year',
                'DZS_installation_date', 'DZS_last_data_date', 'Richtung'
            ])
            writer.writeheader()

            for counter in self.counters_data:
                # Calculate total days in date range
                start = datetime.strptime(self.start_date, '%Y-%m-%d')
                end = datetime.strptime(self.end_date, '%Y-%m-%d')
                total_days = (end - start).days + 1

                # Common fields for all rows
                base_row = {
                    'Counter_ID_orig': counter['id'],
                    'Counter_Name': counter['name'],
                    'lat': counter['lat'],
                    'lon': counter['lon'],
                    'DZS_Datenquelle': counter.get('DZS_Datenquelle', 'EcoCounter'),
                    'year': counter.get('year', ''),
                    'DZS_installation_date': counter.get('DZS_installation_date', ''),
                    'DZS_last_data_date': counter.get('DZS_last_data_date', ''),
                }

                # Row 1: Richtung=0 (combined/total)
                row_total = base_row.copy()
                row_total['DZS_mean_SR'] = counter.get('DZS_mean_SR', '')
                row_total['DZS_mean_year'] = counter.get('DZS_mean_year', '')
                row_total['Richtung'] = 0
                writer.writerow(row_total)

                # If counter has directional data, add rows for each direction
                directional_counts_SR = counter.get('directional_counts_SR', [])
                directional_counts_year = counter.get('directional_counts_year', [])

                if directional_counts_SR and len(directional_counts_SR) >= 1:
                    # Row 2: Richtung=1 (direction 1)
                    row_dir1 = base_row.copy()
                    row_dir1['DZS_mean_SR'] = int(directional_counts_SR[0] / total_days)
                    row_dir1['DZS_mean_year'] = int(directional_counts_year[0] / 365) if directional_counts_year else ''
                    row_dir1['Richtung'] = 1
                    writer.writerow(row_dir1)

                if directional_counts_SR and len(directional_counts_SR) >= 2:
                    # Row 3: Richtung=2 (direction 2)
                    row_dir2 = base_row.copy()
                    row_dir2['DZS_mean_SR'] = int(directional_counts_SR[1] / total_days)
                    row_dir2['DZS_mean_year'] = int(directional_counts_year[1] / 365) if len(directional_counts_year) >= 2 else ''
                    row_dir2['Richtung'] = 2
                    writer.writerow(row_dir2)

        print(f"CSV data saved to: {filename}")


def fetch_from_discovered(
    start_date: str,
    end_date: str,
    country: str = None,
    org_ids: list = None,
    output_dir: str = ".",
    combined_csv: str = None,
    sqlite_db: str = None,
    download_images: bool = True,
):
    """
    Fetch data for all organizations in discovered_organizations.json.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        country: Filter by country code (e.g., "de")
        org_ids: List of specific org IDs to fetch (None = all)
        output_dir: Directory to save output files
        combined_csv: If set, save all data to a single combined CSV
        sqlite_db: If set, save all data + images to SQLite database
        download_images: Whether to download images (for SQLite mode)
    """
    import os

    discovered_file = "discovered_organizations.json"
    if not os.path.exists(discovered_file):
        print(f"Error: {discovered_file} not found. Run explore_orgs.py first.")
        return

    with open(discovered_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    organizations = data.get('organizations', [])
    if not organizations:
        print("No organizations found in discovered_organizations.json")
        return

    # Filter by country if specified
    if country:
        organizations = [org for org in organizations if org.get('country') == country.lower()]

    # Filter by specific IDs if specified
    if org_ids:
        org_ids_set = set(org_ids)
        organizations = [org for org in organizations if org['id'] in org_ids_set]

    if not organizations:
        print("No organizations match the filter criteria")
        return

    print(f"\n{'='*60}")
    print(f"Fetching data for {len(organizations)} organizations")
    print(f"{'='*60}")
    print(f"Date range: {start_date} to {end_date}")
    if combined_csv:
        print(f"Combined CSV: {combined_csv}")
    if sqlite_db:
        print(f"SQLite database: {sqlite_db}")
        print(f"Download images: {download_images}")
    print(f"{'='*60}\n")

    # Create output directory if needed
    os.makedirs(output_dir, exist_ok=True)

    # Initialize combined CSV if requested
    combined_rows = []
    csv_fieldnames = [
        'Org_ID', 'Org_Name', 'Counter_ID_orig', 'Counter_Name', 'lat', 'lon',
        'DZS_Datenquelle', 'DZS_mean_SR', 'DZS_mean_year', 'year',
        'DZS_installation_date', 'DZS_last_data_date', 'Richtung', 'image_urls'
    ]

    # Initialize SQLite if requested
    db_conn = None
    if sqlite_db:
        db_path = os.path.join(output_dir, sqlite_db) if output_dir != "." else sqlite_db
        db_conn = init_database(db_path)
        print(f"Initialized SQLite database: {db_path}")

    # Process each organization
    for i, org in enumerate(organizations, 1):
        org_id = org['id']
        org_name = org['name']
        print(f"\n[{i}/{len(organizations)}] Processing: {org_name} (ID: {org_id})")
        print("-" * 50)

        try:
            org_tier = org.get('tier', 'legacy')
            subdomain_url = org.get('subdomain_url')

            if org_tier == 'migrated' and subdomain_url:
                # Migrated orgs: use web scraper (legacy API returns 404)
                print(f"  [MIGRATED] Using web scraper for {subdomain_url}")
                from datetime import datetime as _dt
                scraper = EcoCounterScraper(
                    base_url=subdomain_url,
                    start_date=start_date,
                    end_date=end_date
                )
                scraper.scrape()
                counters_data = scraper.counters_data

                # Convert scraper format to API format for uniform processing
                total_days = (_dt.strptime(end_date, '%Y-%m-%d') - _dt.strptime(start_date, '%Y-%m-%d')).days + 1
                for counter in counters_data:
                    counter['id'] = str(counter['id'])
                    dir_counts_sr = counter.pop('directional_counts_SR', [])
                    dir_counts_year = counter.pop('directional_counts_year', [])
                    counter['directional_adt_sr'] = [round(c / total_days) for c in dir_counts_sr]
                    counter['directional_adt_year'] = [round(c / 365) for c in dir_counts_year]
                    counter.setdefault('photo_urls', [])
            else:
                # Legacy/dual orgs: use API
                client = EcoVisioAPI(
                    org_id=str(org_id),
                    start_date=start_date,
                    end_date=end_date
                )
                client.scrape()
                counters_data = client.counters_data

            # Add to combined CSV rows
            if combined_csv or sqlite_db:
                for counter in counters_data:
                    photo_urls = counter.get('photo_urls', [])
                    image_urls_str = ';'.join(photo_urls)

                    base_row = {
                        'Org_ID': org_id,
                        'Org_Name': org_name,
                        'Counter_ID_orig': counter['id'],
                        'Counter_Name': counter['name'],
                        'lat': counter['lat'],
                        'lon': counter['lon'],
                        'DZS_Datenquelle': counter.get('DZS_Datenquelle', 'EcoCounter'),
                        'year': counter.get('year', ''),
                        'DZS_installation_date': counter.get('DZS_installation_date', ''),
                        'DZS_last_data_date': counter.get('DZS_last_data_date', ''),
                        'image_urls': image_urls_str,
                    }

                    # Row for combined (Richtung=0)
                    row_total = base_row.copy()
                    row_total['DZS_mean_SR'] = counter.get('DZS_mean_SR', '')
                    row_total['DZS_mean_year'] = counter.get('DZS_mean_year', '')
                    row_total['Richtung'] = 0
                    combined_rows.append(row_total)

                    # Directional rows
                    dir_adt_sr = counter.get('directional_adt_sr', [])
                    dir_adt_year = counter.get('directional_adt_year', [])

                    if len(dir_adt_sr) >= 1:
                        row_dir1 = base_row.copy()
                        row_dir1['DZS_mean_SR'] = dir_adt_sr[0]
                        row_dir1['DZS_mean_year'] = dir_adt_year[0] if len(dir_adt_year) >= 1 else ''
                        row_dir1['Richtung'] = 1
                        combined_rows.append(row_dir1)

                    if len(dir_adt_sr) >= 2:
                        row_dir2 = base_row.copy()
                        row_dir2['DZS_mean_SR'] = dir_adt_sr[1]
                        row_dir2['DZS_mean_year'] = dir_adt_year[1] if len(dir_adt_year) >= 2 else ''
                        row_dir2['Richtung'] = 2
                        combined_rows.append(row_dir2)

            # Save to SQLite if requested
            if db_conn:
                cursor = db_conn.cursor()

                # Insert organization
                cursor.execute("""
                    INSERT OR REPLACE INTO organizations (id, name, country)
                    VALUES (?, ?, ?)
                """, (org_id, org_name, country or 'unknown'))

                for counter in counters_data:
                    counter_id = int(counter['id'])

                    # Insert counter
                    cursor.execute("""
                        INSERT OR REPLACE INTO counters (id, org_id, name, lat, lon, install_date, last_data_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        counter_id, org_id, counter['name'],
                        counter['lat'], counter['lon'],
                        counter.get('DZS_installation_date', ''),
                        counter.get('DZS_last_data_date', ''),
                    ))

                    # Insert counter data
                    cursor.execute("""
                        INSERT INTO counter_data (counter_id, date_start, date_end, year, adt_range, adt_year, direction)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        counter_id, start_date, end_date,
                        counter.get('year'), counter.get('DZS_mean_SR'), counter.get('DZS_mean_year'), 0,
                    ))

                    # Directional data
                    dir_adt_sr = counter.get('directional_adt_sr', [])
                    dir_adt_year = counter.get('directional_adt_year', [])
                    for idx, (adt_sr, adt_year) in enumerate(zip(dir_adt_sr, dir_adt_year), 1):
                        cursor.execute("""
                            INSERT INTO counter_data (counter_id, date_start, date_end, year, adt_range, adt_year, direction)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (counter_id, start_date, end_date, counter.get('year'), adt_sr, adt_year, idx))

                    # Download and store images
                    if download_images:
                        for idx, url in enumerate(counter.get('photo_urls', [])):
                            try:
                                # Skip if image already exists
                                cursor.execute("SELECT 1 FROM images WHERE counter_id = ? AND url = ?", (counter_id, url))
                                if cursor.fetchone():
                                    print(f"    Image already exists, skipping: {counter['name'][:30]}")
                                    continue

                                print(f"    Downloading image {idx + 1} for {counter['name'][:30]}...")
                                response = requests.get(url, timeout=30)
                                response.raise_for_status()
                                filename = Path(url).name or f"image_{idx}.jpg"
                                content_type = response.headers.get('Content-Type', 'image/jpeg')

                                cursor.execute("""
                                    INSERT INTO images (counter_id, url, filename, content_type, image_data)
                                    VALUES (?, ?, ?, ?, ?)
                                """, (counter_id, url, filename, content_type, response.content))
                            except requests.RequestException as e:
                                print(f"    Error downloading image: {e}")

                db_conn.commit()

        except Exception as e:
            print(f"  Error processing {org_name}: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Write combined CSV
    if combined_csv and combined_rows:
        csv_path = os.path.join(output_dir, combined_csv) if output_dir != "." else combined_csv
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=csv_fieldnames)
            writer.writeheader()
            writer.writerows(combined_rows)
        print(f"\nCombined CSV saved to: {csv_path}")
        print(f"  Total rows: {len(combined_rows)}")

    # Close SQLite
    if db_conn:
        db_conn.close()
        print(f"\nSQLite database saved: {sqlite_db}")

        # Print summary
        conn = sqlite3.connect(os.path.join(output_dir, sqlite_db) if output_dir != "." else sqlite_db)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM organizations")
        org_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM counters")
        counter_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM images")
        image_count = cursor.fetchone()[0]
        cursor.execute("SELECT COALESCE(SUM(LENGTH(image_data)), 0) FROM images")
        total_size = cursor.fetchone()[0]
        conn.close()

        print(f"  Organizations: {org_count}")
        print(f"  Counters: {counter_count}")
        print(f"  Images: {image_count} ({total_size / 1024 / 1024:.1f} MB)")

    print(f"\n{'='*60}")
    print(f"Completed! Processed {len(organizations)} organizations")
    print(f"{'='*60}")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Scrape bicycle counter data from eco-counter websites or Eco-Visio API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape data for Stadt Augsburg via web scraping (May-September 2025)
  python scraper.py https://stadtaugsburg.eco-counter.com --start 2025-05-01 --end 2025-09-30

  # Fetch data via Eco-Visio API using organization ID
  python scraper.py --api 5417 --start 2025-05-01 --end 2025-09-30

  # Fetch ALL discovered organizations into a SINGLE combined CSV
  python scraper.py --from-discovered --country de --combined-csv germany_all.csv

  # Fetch into SQLite with images
  python scraper.py --from-discovered --country de --sqlite germany_data.db

  # Combined CSV + SQLite (CSV references images by counter_id in SQLite)
  python scraper.py --from-discovered --country de --combined-csv germany.csv --sqlite germany.db

  # Skip image downloads (faster)
  python scraper.py --from-discovered --country de --sqlite germany.db --no-images

  # Fetch for specific org IDs
  python scraper.py --from-discovered --org-ids 5417 4701 4702 --combined-csv subset.csv

  # Find organization ID: Look at Eco-Visio public pages, the ID is in the URL
  # e.g., https://www.eco-visio.net/api/aladdin/1.0.0/pbl/publicwebpageplus/5417
  #       5417 is the organization ID for Augsburg
        """
    )

    parser.add_argument(
        'url',
        nargs='?',
        help='Base URL of the eco-counter site (e.g., https://stadtaugsburg.eco-counter.com)'
    )
    parser.add_argument(
        '--api',
        metavar='ORG_ID',
        help='Use Eco-Visio API with organization ID instead of web scraping'
    )
    parser.add_argument(
        '--from-discovered',
        action='store_true',
        help='Fetch data for organizations in discovered_organizations.json'
    )
    parser.add_argument(
        '--country',
        help='Filter discovered orgs by country code (e.g., "de" for Germany)'
    )
    parser.add_argument(
        '--org-ids',
        type=int,
        nargs='+',
        help='Specific organization IDs to fetch from discovered list'
    )
    parser.add_argument(
        '--output-dir',
        default='.',
        help='Output directory for --from-discovered mode (default: current directory)'
    )
    parser.add_argument(
        '--combined-csv',
        metavar='FILENAME',
        help='Save all data to a single combined CSV (for --from-discovered)'
    )
    parser.add_argument(
        '--start',
        default='2025-05-01',
        help='Start date in YYYY-MM-DD format (default: 2025-05-01)'
    )
    parser.add_argument(
        '--end',
        default='2025-09-30',
        help='End date in YYYY-MM-DD format (default: 2025-09-30)'
    )
    parser.add_argument(
        '--json',
        help='Output JSON filename (default: {name}_counters_data.json)'
    )
    parser.add_argument(
        '--csv',
        help='Output CSV filename (default: {name}_counters_data.csv)'
    )
    parser.add_argument(
        '--sqlite',
        nargs='?',
        const=True,
        default=False,
        help='Save to SQLite database with images (optionally specify filename)'
    )
    parser.add_argument(
        '--no-images',
        action='store_true',
        help='Skip downloading images when using --sqlite'
    )

    args = parser.parse_args()

    # Handle --from-discovered mode
    if args.from_discovered:
        # Determine sqlite filename
        sqlite_db = None
        if args.sqlite:
            sqlite_db = args.sqlite if isinstance(args.sqlite, str) else 'discovered_data.db'

        fetch_from_discovered(
            start_date=args.start,
            end_date=args.end,
            country=args.country,
            org_ids=args.org_ids,
            output_dir=args.output_dir,
            combined_csv=args.combined_csv,
            sqlite_db=sqlite_db,
            download_images=not args.no_images,
        )
        return

    # Validate arguments for other modes
    if not args.api and not args.url:
        parser.error("Either URL, --api ORG_ID, or --from-discovered is required")

    if args.api:
        # Use API mode
        client = EcoVisioAPI(
            org_id=args.api,
            start_date=args.start,
            end_date=args.end
        )
        client.scrape()

        # Save outputs
        if args.sqlite:
            sqlite_file = args.sqlite if isinstance(args.sqlite, str) else None
            client.save_sqlite(sqlite_file, download_images=not args.no_images)
        else:
            client.save_json(args.json)
            client.save_csv(args.csv)
    else:
        # Use web scraping mode (requires playwright)
        scraper = EcoCounterScraper(
            base_url=args.url,
            start_date=args.start,
            end_date=args.end
        )
        scraper.scrape()
        scraper.save_json(args.json)
        scraper.save_csv(args.csv)


if __name__ == "__main__":
    main()
