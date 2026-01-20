"""
Eco-Counter Data Scraper for Stadt Augsburg

This script extracts bicycle counter data from the eco-counter website for any city if the URL format is in xxx.eco-counter.com -format..
It retrieves counter metadata (ID, name, coordinates) and daily average counts (ADT).

Output formats:
- JSON: Raw data output
- CSV: Formatted with columns: Counter_ID_orig, Counter_Name, lat, lon, DZS_mean_SR
"""

import json
import csv
import re
from datetime import datetime
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright, Page


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

    def extract_counter_metadata(self, page: Page) -> List[Dict]:
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

                // Find script containing site data
                for (let i = 0; i < scripts.length; i++) {
                    const text = scripts[i].textContent || '';
                    if (text.indexOf('Wagenhalsstra') > -1) {
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

                    // Check if it's a site ID (100... or 300...)
                    if (id.length >= 9 && (id.startsWith('100') || id.startsWith('300'))) {
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
                    if (sites.length > 20) break;
                }

                return sites;
            }
        """)

        print(f"Found {len(sites)} counters")
        return sites

    def extract_counter_details(self, page: Page, site_id: str) -> Dict:
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
                            const countsPattern = /\\\\"counts\\\\":(\d+)/g;
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
                            const countsPattern = /\\\\"counts\\\\":(\d+)/g;
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
                                const countsPattern = /\\\\"counts\\\\":(\d+)/g;
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
                                const countsPattern = /\\\\"counts\\\\":(\d+)/g;
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


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Scrape bicycle counter data from eco-counter websites',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape data for Stadt Augsburg (May-September 2025)
  python scraper.py https://stadtaugsburg.eco-counter.com --start 2025-05-01 --end 2025-09-30

  # Scrape full year 2025 data
  python scraper.py https://stadtaugsburg.eco-counter.com --start 2025-01-01 --end 2025-12-31

  # Scrape data for a different city
  python scraper.py https://paris.eco-counter.com --start 2025-05-01 --end 2025-09-30
        """
    )

    parser.add_argument(
        'url',
        help='Base URL of the eco-counter site (e.g., https://stadtaugsburg.eco-counter.com)'
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
        help='Output JSON filename (default: {cityname}_counters_data.json)'
    )
    parser.add_argument(
        '--csv',
        help='Output CSV filename (default: {cityname}_counters_data.csv)'
    )

    args = parser.parse_args()

    # Create scraper instance
    scraper = EcoCounterScraper(
        base_url=args.url,
        start_date=args.start,
        end_date=args.end
    )

    # Run scraper
    scraper.scrape()

    # Save outputs
    scraper.save_json(args.json)
    scraper.save_csv(args.csv)


if __name__ == "__main__":
    main()
