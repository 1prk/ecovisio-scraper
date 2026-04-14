"""
Batch Subdomain Detector & Scraper for Eco-Counter

Processes a list of subdomains:
1. Classifies each page type
2. For counter_map sites, extracts full counter data (like scraper.py)

Page types:
- counter_map: Has counter/site data (will be scraped)
- data_portal: Data portal or dashboard
- corporate: Marketing/corporate page
- api_docs: API documentation
- status_page: Status/uptime page
- language_redirect: Language selector/redirect page
- error: Page not found, error, or unreachable
- unknown: Could not classify
"""

import asyncio
import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout


@dataclass
class CounterData:
    id: str
    name: str
    lat: float
    lon: float
    DZS_mean_SR: Optional[int] = None
    DZS_mean_year: Optional[int] = None
    DZS_installation_date: Optional[str] = None
    DZS_last_data_date: Optional[str] = None
    directions: list = field(default_factory=list)
    directional_counts_SR: list = field(default_factory=list)
    directional_counts_year: list = field(default_factory=list)


@dataclass
class PageClassification:
    subdomain: str
    url: str
    page_type: str
    confidence: str  # high, medium, low
    details: dict = field(default_factory=dict)
    counters: list = field(default_factory=list)  # List of CounterData
    error: Optional[str] = None


class SubdomainDetector:
    """Detects and classifies eco-counter subdomain page types."""

    def __init__(self, timeout_ms: int = 15000, start_date: str = "2025-05-01", end_date: str = "2025-09-30"):
        self.timeout_ms = timeout_ms
        self.start_date = start_date
        self.end_date = end_date

    async def classify_page(self, page: Page, subdomain: str, extract_details: bool = True) -> PageClassification:
        """
        Classify a single subdomain page and optionally extract counter details.
        """
        url = f"https://{subdomain}"
        result = PageClassification(subdomain=subdomain, url=url, page_type="unknown", confidence="low")

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

            if response is None:
                result.page_type = "error"
                result.error = "No response"
                return result

            status = response.status
            result.details["status_code"] = status

            if status >= 400:
                result.page_type = "error"
                result.error = f"HTTP {status}"
                result.confidence = "high"
                return result

            await page.wait_for_timeout(2000)

            html = await page.content()
            title = await page.title()
            result.details["title"] = title
            result.details["final_url"] = page.url

            # Detection: Counter Map Site
            counter_data = await self._detect_counter_map(page, html)
            if counter_data["is_counter_map"]:
                result.page_type = "counter_map"
                result.confidence = "high"
                result.details["counter_count"] = counter_data["counter_count"]
                result.details["has_map"] = counter_data["has_map"]

                # Extract full counter details if requested
                if extract_details and counter_data["counters"]:
                    result.counters = await self._extract_all_counter_details(
                        page, subdomain, counter_data["counters"]
                    )
                else:
                    # Just store basic metadata
                    result.counters = [
                        CounterData(id=c["id"], name=c["name"], lat=c["lat"], lon=c["lon"])
                        for c in counter_data["counters"]
                    ]
                return result

            # Other detections...
            if self._detect_data_portal(html, title):
                result.page_type = "data_portal"
                result.confidence = "medium"
                return result

            if self._detect_api_docs(html, title, subdomain):
                result.page_type = "api_docs"
                result.confidence = "high"
                return result

            if self._detect_status_page(html, title, subdomain):
                result.page_type = "status_page"
                result.confidence = "high"
                return result

            if self._detect_language_redirect(html, title, subdomain):
                result.page_type = "language_redirect"
                result.confidence = "medium"
                return result

            if self._detect_corporate(html, title):
                result.page_type = "corporate"
                result.confidence = "medium"
                return result

            if counter_data["has_map"]:
                result.page_type = "counter_map"
                result.confidence = "low"
                result.details["has_map"] = True
                result.details["counter_count"] = 0
                return result

        except PlaywrightTimeout:
            result.page_type = "error"
            result.error = "Timeout"
            result.confidence = "high"
        except Exception as e:
            result.page_type = "error"
            result.error = str(e)[:100]
            result.confidence = "high"

        return result

    async def _detect_counter_map(self, page: Page, html: str) -> dict:
        """Detect if page is a counter map site with embedded site data."""
        result = {"is_counter_map": False, "counter_count": 0, "has_map": False, "counters": []}

        map_indicators = ["mapboxgl", "leaflet", 'data-testid="map', 'class="map', 'id="map']
        result["has_map"] = any(ind in html.lower() for ind in map_indicators)

        try:
            sites = await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script');
                    let content = '';

                    for (let i = 0; i < scripts.length; i++) {
                        const text = scripts[i].textContent || '';
                        if (text.indexOf('"sites":') > -1 || text.indexOf('\\\\"location\\\\":') > -1) {
                            content = text;
                            break;
                        }
                    }

                    if (!content) {
                        content = document.body?.innerHTML || '';
                    }

                    const sites = [];
                    const searchStr = String.fromCharCode(92, 34, 105, 100, 92, 34, 58);
                    let pos = 0;

                    while (true) {
                        const idxFound = content.indexOf(searchStr, pos);
                        if (idxFound === -1) break;

                        const idStart = idxFound + searchStr.length;
                        const idEnd = content.indexOf(',', idStart);
                        const id = content.substring(idStart, idEnd).trim();

                        if (id.length >= 8 && /^[13]\\d{8,}/.test(id)) {
                            const chunk = content.substring(idxFound, idxFound + 600);

                            const namePattern = String.fromCharCode(92, 34, 110, 97, 109, 101, 92, 34, 58, 92, 34);
                            const nameIdx = chunk.indexOf(namePattern);
                            if (nameIdx > -1) {
                                const nameStart = nameIdx + namePattern.length;
                                const nameEnd = chunk.indexOf(String.fromCharCode(92, 34), nameStart);
                                const name = chunk.substring(nameStart, nameEnd);

                                const latPattern = String.fromCharCode(92, 34, 108, 97, 116, 92, 34, 58);
                                const latIdx = chunk.indexOf(latPattern);
                                if (latIdx > -1) {
                                    const latStart = latIdx + latPattern.length;
                                    const lat = parseFloat(chunk.substring(latStart, chunk.indexOf(',', latStart)));

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

            if sites and len(sites) > 0:
                result["is_counter_map"] = True
                result["counter_count"] = len(sites)
                result["counters"] = sites
        except Exception:
            pass

        counter_indicators = ['data-testid="site-', 'data-testid="counter-', 'data-testid="data-section-kpi', '"siteId"', '"counterId"']
        if any(ind in html for ind in counter_indicators):
            result["is_counter_map"] = True

        return result

    async def _extract_all_counter_details(self, page: Page, subdomain: str, counters: list) -> list[CounterData]:
        """Extract full details for all counters (ADT, dates, directions)."""
        base_url = f"https://{subdomain}"
        results = []

        for i, counter in enumerate(counters):
            print(f"    [{i+1}/{len(counters)}] {counter['name'][:30]}...", end=" ", flush=True)

            counter_data = CounterData(
                id=counter["id"],
                name=counter["name"],
                lat=counter["lat"],
                lon=counter["lon"]
            )

            try:
                # Get ADT for date range
                url = f"{base_url}/site/{counter['id']}?startDate={self.start_date}&endDate={self.end_date}"
                await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                await page.wait_for_timeout(2000)

                # Extract ADT
                adt_element = await page.query_selector('[data-testid="data-section-kpi-adt-value"]')
                if adt_element:
                    adt_text = await adt_element.text_content()
                    cleaned = adt_text.strip().replace(',', '').replace('.', '')
                    counter_data.DZS_mean_SR = int(cleaned) if cleaned.isdigit() else None

                # Extract dates
                dates = await page.evaluate("""
                    () => {
                        const fullHtml = document.documentElement.innerHTML;
                        const convertDate = (dateStr) => {
                            const parts = dateStr.split('/');
                            if (parts.length === 3) return `${parts[1]}.${parts[0]}.${parts[2]}`;
                            return dateStr;
                        };
                        const installMatch = fullHtml.match(/Installation.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                        const installDate = installMatch ? convertDate(installMatch[1]) : null;
                        let lastMatch = fullHtml.match(/Letzte Daten.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                        if (!lastMatch) lastMatch = fullHtml.match(/last-data-date.*?(\\d{1,2}\\/\\d{1,2}\\/\\d{4})/s);
                        const lastDate = lastMatch ? convertDate(lastMatch[1]) : null;
                        return {installation: installDate, lastData: lastDate};
                    }
                """)
                if dates:
                    counter_data.DZS_installation_date = dates.get("installation")
                    counter_data.DZS_last_data_date = dates.get("lastData")

                # Extract directional data
                direction_data = await page.evaluate("""
                    () => {
                        const directions = new Set();
                        const allText = document.body.textContent;
                        const riPattern = /Ri\\. ([^(]+) \\((In|Out)\\)/g;
                        let match;
                        while ((match = riPattern.exec(allText)) !== null) {
                            directions.add('Ri. ' + match[1].trim() + ' (' + match[2] + ')');
                        }
                        if (allText.includes('IN (In)') && allText.includes('OUT (Out)')) {
                            directions.add('IN');
                            directions.add('OUT');
                        }
                        const directionNames = Array.from(directions).slice(0, 2);
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
                                let m;
                                while ((m = countsPattern.exec(inCountsMatch[1])) !== null) {
                                    inTotalCounts += parseInt(m[1]);
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
                                let m;
                                while ((m = countsPattern.exec(outCountsMatch[1])) !== null) {
                                    outTotalCounts += parseInt(m[1]);
                                }
                            }
                            directionalCounts.push(outTotalCounts);
                        }
                        return {names: directionNames, counts: directionalCounts};
                    }
                """)
                counter_data.directions = direction_data.get("names", [])
                counter_data.directional_counts_SR = direction_data.get("counts", [])

                # Get yearly ADT
                year_url = f"{base_url}/site/{counter['id']}?startDate=2025-01-01&endDate=2025-12-31"
                await page.goto(year_url, wait_until="networkidle", timeout=self.timeout_ms)
                await page.wait_for_timeout(1500)

                adt_year_element = await page.query_selector('[data-testid="data-section-kpi-adt-value"]')
                if adt_year_element:
                    adt_year_text = await adt_year_element.text_content()
                    cleaned_year = adt_year_text.strip().replace(',', '').replace('.', '')
                    counter_data.DZS_mean_year = int(cleaned_year) if cleaned_year.isdigit() else None

                # Get yearly directional counts
                if counter_data.directions:
                    year_direction_counts = await page.evaluate("""
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
                                    let m;
                                    while ((m = countsPattern.exec(inCountsMatch[1])) !== null) {
                                        inTotalCounts += parseInt(m[1]);
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
                                    let m;
                                    while ((m = countsPattern.exec(outCountsMatch[1])) !== null) {
                                        outTotalCounts += parseInt(m[1]);
                                    }
                                }
                                directionalCounts.push(outTotalCounts);
                            }
                            return directionalCounts;
                        }
                    """)
                    counter_data.directional_counts_year = year_direction_counts or []

                print(f"ADT={counter_data.DZS_mean_SR}")

            except Exception as e:
                print(f"error: {str(e)[:50]}")

            results.append(counter_data)

        return results

    def _detect_data_portal(self, html: str, title: str) -> bool:
        indicators = ["data portal", "open data", "dataset", "download data", "data catalog", "public data"]
        text = (html + " " + title).lower()
        return any(ind in text for ind in indicators)

    def _detect_api_docs(self, html: str, title: str, subdomain: str) -> bool:
        if "api" in subdomain or "developer" in subdomain:
            return True
        indicators = ["api documentation", "api reference", "swagger", "openapi", "rest api", "developer portal", "authentication", "endpoints"]
        text = (html + " " + title).lower()
        return sum(1 for ind in indicators if ind in text) >= 2

    def _detect_status_page(self, html: str, title: str, subdomain: str) -> bool:
        if "status" in subdomain:
            return True
        indicators = ["system status", "service status", "operational", "uptime", "incident", "statuspage"]
        text = (html + " " + title).lower()
        return any(ind in text for ind in indicators)

    def _detect_language_redirect(self, html: str, title: str, subdomain: str) -> bool:
        lang_patterns = ["^(de|en|es|fr|it|pt|zh|ja|ko|nl|pl|ru)$", "^npu\\.(de|en|es|it|pt)$"]
        for pattern in lang_patterns:
            if re.match(pattern, subdomain.split('.')[0]):
                return True
        indicators = ["select language", "choose language", "select your country", "choose your region"]
        text = (html + " " + title).lower()
        return any(ind in text for ind in indicators)

    def _detect_corporate(self, html: str, title: str) -> bool:
        indicators = ["eco-counter", "about us", "contact us", "our solutions", "products", "services", "case studies", "news", "press", "careers", "bicycle counting", "pedestrian counting"]
        text = (html + " " + title).lower()
        return sum(1 for ind in indicators if ind in text) >= 3


def save_counters_csv(results: list[PageClassification], filename: str, start_date: str, end_date: str):
    """Save all counter data to CSV (same format as scraper.py)."""
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    total_days = (end - start).days + 1

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'subdomain', 'Counter_ID_orig', 'Counter_Name', 'lat', 'lon',
            'DZS_Datenquelle', 'DZS_mean_SR', 'DZS_mean_year', 'year',
            'DZS_installation_date', 'DZS_last_data_date', 'Richtung'
        ])
        writer.writeheader()

        for result in results:
            if result.page_type != "counter_map":
                continue

            subdomain = result.subdomain
            for counter in result.counters:
                base_row = {
                    'subdomain': subdomain,
                    'Counter_ID_orig': counter.id,
                    'Counter_Name': counter.name,
                    'lat': counter.lat,
                    'lon': counter.lon,
                    'DZS_Datenquelle': 'EcoCounter',
                    'year': int(start_date.split('-')[0]),
                    'DZS_installation_date': counter.DZS_installation_date or '',
                    'DZS_last_data_date': counter.DZS_last_data_date or '',
                }

                # Row with Richtung=0 (total)
                row_total = base_row.copy()
                row_total['DZS_mean_SR'] = counter.DZS_mean_SR or ''
                row_total['DZS_mean_year'] = counter.DZS_mean_year or ''
                row_total['Richtung'] = 0
                writer.writerow(row_total)

                # Directional rows
                if counter.directional_counts_SR and len(counter.directional_counts_SR) >= 1:
                    row_dir1 = base_row.copy()
                    row_dir1['DZS_mean_SR'] = int(counter.directional_counts_SR[0] / total_days) if counter.directional_counts_SR[0] else ''
                    row_dir1['DZS_mean_year'] = int(counter.directional_counts_year[0] / 365) if counter.directional_counts_year else ''
                    row_dir1['Richtung'] = 1
                    writer.writerow(row_dir1)

                if counter.directional_counts_SR and len(counter.directional_counts_SR) >= 2:
                    row_dir2 = base_row.copy()
                    row_dir2['DZS_mean_SR'] = int(counter.directional_counts_SR[1] / total_days) if counter.directional_counts_SR[1] else ''
                    row_dir2['DZS_mean_year'] = int(counter.directional_counts_year[1] / 365) if len(counter.directional_counts_year) >= 2 else ''
                    row_dir2['Richtung'] = 2
                    writer.writerow(row_dir2)

    print(f"Counter data CSV saved to: {filename}")


async def process_subdomains(
    subdomains: list[str],
    concurrency: int = 3,
    output_file: str = "subdomain_classification.json",
    start_date: str = "2025-05-01",
    end_date: str = "2025-09-30",
    extract_details: bool = True
) -> list[PageClassification]:
    """Process multiple subdomains concurrently."""
    detector = SubdomainDetector(start_date=start_date, end_date=end_date)
    results: list[PageClassification] = []

    unique_subdomains = list(dict.fromkeys(subdomains))
    total = len(unique_subdomains)

    print(f"Processing {total} unique subdomains (from {len(subdomains)} total)")
    print(f"Date range: {start_date} to {end_date}")
    print(f"Extract counter details: {extract_details}")
    print(f"Concurrency: {concurrency}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        semaphore = asyncio.Semaphore(concurrency)

        async def process_one(subdomain: str, index: int) -> PageClassification:
            async with semaphore:
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    print(f"\n[{index + 1}/{total}] {subdomain}...", flush=True)
                    result = await detector.classify_page(page, subdomain, extract_details=extract_details)
                    status = f"  -> {result.page_type}"
                    if result.error:
                        status += f" ({result.error})"
                    elif result.details.get("counter_count"):
                        status += f" ({result.details['counter_count']} counters)"
                    print(status)
                    return result
                finally:
                    await context.close()

        tasks = [process_one(sub, i) for i, sub in enumerate(unique_subdomains)]
        results = await asyncio.gather(*tasks)

        await browser.close()

    # Save JSON results
    output_data = {
        "total_processed": len(results),
        "date_range": {"start": start_date, "end": end_date},
        "summary": {},
        "results": []
    }

    for result in results:
        ptype = result.page_type
        output_data["summary"][ptype] = output_data["summary"].get(ptype, 0) + 1

        result_dict = {
            "subdomain": result.subdomain,
            "url": result.url,
            "page_type": result.page_type,
            "confidence": result.confidence,
            "details": result.details,
            "error": result.error,
            "counters": [
                {
                    "id": c.id, "name": c.name, "lat": c.lat, "lon": c.lon,
                    "DZS_mean_SR": c.DZS_mean_SR, "DZS_mean_year": c.DZS_mean_year,
                    "DZS_installation_date": c.DZS_installation_date,
                    "DZS_last_data_date": c.DZS_last_data_date,
                    "directions": c.directions,
                    "directional_counts_SR": c.directional_counts_SR,
                    "directional_counts_year": c.directional_counts_year
                }
                for c in result.counters
            ] if result.counters else []
        }
        output_data["results"].append(result_dict)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("Summary:")
    for ptype, count in sorted(output_data["summary"].items(), key=lambda x: -x[1]):
        print(f"  {ptype}: {count}")
    print(f"\nResults saved to: {output_file}")

    # Save CSV with counter data
    csv_file = output_file.replace(".json", "_counters.csv")
    save_counters_csv(results, csv_file, start_date, end_date)

    # Save list of counter_map subdomains
    counter_maps = [r for r in results if r.page_type == "counter_map"]
    if counter_maps:
        list_file = output_file.replace(".json", "_counter_maps.txt")
        with open(list_file, "w") as f:
            for r in counter_maps:
                f.write(f"{r.subdomain}\t{len(r.counters)} counters\n")
        print(f"Counter map list saved to: {list_file}")

    return results


def load_subdomains(filepath: str) -> list[str]:
    """Load subdomains from a file (one per line)."""
    subdomains = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if "://" in line:
                    match = re.search(r"https?://([^/]+)", line)
                    if match:
                        line = match.group(1)
                subdomains.append(line)
    return subdomains


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Detect and scrape eco-counter subdomains")
    parser.add_argument("input_file", nargs="?", default="subs.txt", help="Input file with subdomains")
    parser.add_argument("-o", "--output", default="subdomain_classification.json", help="Output JSON file")
    parser.add_argument("-c", "--concurrency", type=int, default=3, help="Concurrent requests (default: 3)")
    parser.add_argument("--start", default="2025-05-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-09-30", help="End date YYYY-MM-DD")
    parser.add_argument("--no-details", action="store_true", help="Skip extracting full counter details")

    args = parser.parse_args()

    subdomains = load_subdomains(args.input_file)
    if not subdomains:
        print(f"No subdomains found in {args.input_file}")
        return

    await process_subdomains(
        subdomains,
        concurrency=args.concurrency,
        output_file=args.output,
        start_date=args.start,
        end_date=args.end,
        extract_details=not args.no_details
    )


if __name__ == "__main__":
    asyncio.run(main())
