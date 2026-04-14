"""
Eco-Visio Organization Explorer (Async Version)

Safely explores Eco-Visio API to find organizations with bicycle counters.
Queries both the legacy eco-visio.net API and the migration API at
api.eco-counter.com to detect organizations across all tiers (legacy, dual, migrated).
Uses async/await for concurrent requests with exponential backoff.

Usage:
    # Explore IDs 1-1000 (default: German counters only)
    uv run python explore_orgs.py --start 1 --end 1000

    # Faster with more concurrent requests (careful!)
    uv run python explore_orgs.py --start 1 --end 1000 --concurrency 10

    # Resume from where you left off
    uv run python explore_orgs.py --start 1 --end 10000 --resume

    # Save to SQLite database
    uv run python explore_orgs.py --start 4000 --end 5000 --sqlite

    # Legacy-only scan (skip migration API)
    uv run python explore_orgs.py --start 4000 --end 5000 --no-migration
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


def init_explore_database(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database for exploration results."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY,
            name TEXT,
            country TEXT,
            counter_count INTEGER,
            discovered_at TEXT,
            logo_url TEXT,
            subdomain_url TEXT,
            tier TEXT
        );

        CREATE TABLE IF NOT EXISTS counter_previews (
            id INTEGER PRIMARY KEY,
            org_id INTEGER,
            name TEXT,
            lat REAL,
            lon REAL,
            FOREIGN KEY (org_id) REFERENCES organizations(id)
        );

        CREATE INDEX IF NOT EXISTS idx_org_country ON organizations(country);
    """)

    # Migrate existing databases: add new columns if missing
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(organizations)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'subdomain_url' not in existing_columns:
        conn.execute("ALTER TABLE organizations ADD COLUMN subdomain_url TEXT")
    if 'tier' not in existing_columns:
        conn.execute("ALTER TABLE organizations ADD COLUMN tier TEXT")

    conn.commit()
    return conn


class EcoVisioExplorer:
    """Async explorer for Eco-Visio API."""

    BASE_URL = "https://www.eco-visio.net/api/aladdin/1.0.0/pbl/publicwebpageplus"
    MIGRATION_API_URL = "https://api.eco-counter.com/api/v2/pages"

    # Output files
    RESULTS_FILE = "discovered_organizations.json"
    PROGRESS_FILE = ".explore_progress.json"

    def __init__(
        self,
        concurrency: int = 5,
        delay_base: float = 0.5,
        country_filter: str = None,
        timeout: int = 15,
        verbose: bool = False,
        check_migration: bool = True,
    ):
        """
        Initialize explorer.

        Args:
            concurrency: Maximum concurrent requests
            delay_base: Base delay between batches (seconds)
            country_filter: Filter by country code (e.g., "de" for Germany)
            timeout: Request timeout in seconds
            verbose: Print status for every ID
            check_migration: Also query migration API for subdomain mappings
        """
        self.concurrency = concurrency
        self.delay_base = delay_base
        self.country_filter = country_filter.lower() if country_filter else None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.verbose = verbose
        self.check_migration = check_migration

        # Legacy API backoff state
        self.backoff_until = 0
        self.backoff_multiplier = 1

        # Migration API backoff state
        self.migration_backoff_until = 0
        self.migration_backoff_multiplier = 1

        # Statistics
        self.stats = {
            'checked': 0,
            'found': 0,
            'filtered': 0,
            'errors': 0,
            'rate_limited': 0,
            'legacy_only': 0,
            'dual_mode': 0,
            'migrated_only': 0,
        }

        # Thread-safe lock for results
        self.lock = asyncio.Lock()

        # Load existing results
        self.results = self._load_results()

    def _load_results(self) -> dict:
        """Load existing results from file."""
        if os.path.exists(self.RESULTS_FILE):
            try:
                with open(self.RESULTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {'organizations': [], 'last_updated': None}

    def _save_results(self):
        """Save results to file."""
        self.results['last_updated'] = datetime.now().isoformat()
        with open(self.RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)

    def _load_progress(self) -> int:
        """Load last checked ID from progress file."""
        if os.path.exists(self.PROGRESS_FILE):
            try:
                with open(self.PROGRESS_FILE, 'r') as f:
                    data = json.load(f)
                    return data.get('last_id', 0)
            except (json.JSONDecodeError, IOError):
                pass
        return 0

    def _save_progress(self, org_id: int):
        """Save progress to file."""
        with open(self.PROGRESS_FILE, 'w') as f:
            json.dump({'last_id': org_id, 'timestamp': datetime.now().isoformat()}, f)

    async def _check_backoff(self):
        """Wait if we're in backoff period."""
        now = asyncio.get_event_loop().time()
        if now < self.backoff_until:
            wait_time = self.backoff_until - now
            print(f"\n⏳ Rate limited - backing off for {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)

    def _trigger_backoff(self):
        """Trigger exponential backoff."""
        self.backoff_multiplier = min(self.backoff_multiplier * 2, 64)
        backoff_time = self.backoff_multiplier * (1 + random.random())
        self.backoff_until = asyncio.get_event_loop().time() + backoff_time
        self.stats['rate_limited'] += 1

    def _reset_backoff(self):
        """Reset backoff after successful requests."""
        self.backoff_multiplier = 1

    async def _check_migration_backoff(self):
        """Wait if we're in migration API backoff period."""
        now = asyncio.get_event_loop().time()
        if now < self.migration_backoff_until:
            wait_time = self.migration_backoff_until - now
            print(f"\n⏳ Migration API rate limited - backing off for {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)

    def _trigger_migration_backoff(self):
        """Trigger exponential backoff for migration API."""
        self.migration_backoff_multiplier = min(self.migration_backoff_multiplier * 2, 64)
        backoff_time = self.migration_backoff_multiplier * (1 + random.random())
        self.migration_backoff_until = asyncio.get_event_loop().time() + backoff_time
        self.stats['rate_limited'] += 1

    def _reset_migration_backoff(self):
        """Reset migration API backoff after successful requests."""
        self.migration_backoff_multiplier = 1

    # Country bounding boxes: (min_lat, max_lat, min_lon, max_lon)
    COUNTRY_BBOXES = {
        'de': (47.27, 55.06, 5.87, 15.04),   # Germany
        'at': (46.37, 49.02, 9.53, 17.16),    # Austria
        'ch': (45.82, 47.81, 5.96, 10.49),    # Switzerland
        'fr': (41.36, 51.09, -5.14, 9.56),    # France
        'nl': (50.75, 53.47, 3.36, 7.21),     # Netherlands
        'be': (49.50, 51.50, 2.55, 6.40),     # Belgium
        'dk': (54.56, 57.75, 8.08, 15.20),    # Denmark
        'se': (55.34, 69.06, 11.11, 24.17),   # Sweden
        'no': (57.96, 71.19, 4.64, 31.08),    # Norway
        'gb': (49.96, 58.64, -7.57, 1.68),    # UK
        'es': (36.00, 43.79, -9.30, 3.33),    # Spain
        'it': (36.65, 47.09, 6.63, 18.52),    # Italy
    }

    @staticmethod
    def _extract_tenant_name(subdomain_url: str) -> str:
        """Extract tenant name from subdomain URL and title-case it.

        e.g. 'https://hessen-mobil.eco-counter.com' -> 'Hessen Mobil'
        """
        hostname = urlparse(subdomain_url).hostname or ""
        tenant = hostname.split('.')[0]
        return tenant.replace('-', ' ').title()

    @classmethod
    def _infer_country_from_coords(cls, counters: list[dict]) -> str | None:
        """Infer country code from counter coordinates using bounding boxes.

        Uses majority voting: the country that contains the most counters wins.
        Returns:
            Country code if matched, 'other' if coordinates exist but match
            no known bbox, None if no counters have valid coordinates.
        """
        if not counters:
            return None

        has_coords = False
        votes: dict[str, int] = {}
        for counter in counters:
            lat, lon = counter.get('lat'), counter.get('lon')
            if lat is None or lon is None:
                continue
            has_coords = True
            for code, (min_lat, max_lat, min_lon, max_lon) in cls.COUNTRY_BBOXES.items():
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    votes[code] = votes.get(code, 0) + 1

        if not votes:
            return 'other' if has_coords else None
        return max(votes, key=votes.get)

    async def check_organization(self, session: aiohttp.ClientSession, org_id: int) -> tuple[dict | None, str]:
        """
        Check if an organization ID exists and has counters.

        Returns:
            Tuple of (organization info dict or None, status message)
        """
        url = f"{self.BASE_URL}/{org_id}"

        # Check if we need to wait for backoff
        await self._check_backoff()

        try:
            async with session.get(url) as response:
                # Rate limited
                if response.status == 429:
                    self._trigger_backoff()
                    return None, "rate limited"

                # Not found or forbidden
                if response.status in (404, 403, 401):
                    self._reset_backoff()
                    return None, "no response"

                # Server error
                if response.status >= 500:
                    return None, f"server error ({response.status})"

                data = await response.json()
                self._reset_backoff()

                # Empty response
                if not data or not isinstance(data, list) or len(data) == 0:
                    return None, "empty response"

                # Extract organization info from first counter
                first_counter = data[0]
                org_info = {
                    'id': org_id,
                    'name': first_counter.get('nomOrganisme', f'Unknown ({org_id})'),
                    'country': first_counter.get('pays', 'unknown'),
                    'counter_count': len(data),
                    'counters': [
                        {
                            'id': c.get('idPdc'),
                            'name': c.get('nom'),
                            'lat': c.get('lat'),
                            'lon': c.get('lon'),
                        }
                        for c in data
                    ],
                    'discovered_at': datetime.now().isoformat(),
                }

                # Apply country filter
                if self.country_filter and org_info['country'] != self.country_filter:
                    return None, f"filtered ({org_info['country']}: {org_info['name'][:30]})"

                return org_info, "found"

        except asyncio.TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError as e:
            self.stats['errors'] += 1
            return None, f"error: {type(e).__name__}"
        except Exception as e:
            self.stats['errors'] += 1
            return None, f"error: {e}"

    async def fetch_counters_from_subdomain(self, session: aiohttp.ClientSession, subdomain_url: str) -> list[dict]:
        """
        Fetch counter metadata from a migrated subdomain's HTML.

        The eco-counter sites embed counter data in <script> tags as escaped JSON
        with patterns like: \"id\":100012345,\"name\":\"Counter Name\",...\"lat\":48.1,\"lon\":10.4

        Returns:
            List of counter dicts with keys: id, name, lat, lon
        """
        try:
            async with session.get(subdomain_url, timeout=self.timeout) as response:
                if response.status != 200:
                    return []
                html = await response.text()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return []

        counters = []
        # The escaped JSON uses literal \" sequences in the HTML source
        search_str = '\\"id\\":'
        pos = 0

        while True:
            idx = html.find(search_str, pos)
            if idx == -1:
                break

            id_start = idx + len(search_str)
            id_end = html.find(',', id_start)
            if id_end == -1:
                break
            site_id = html[id_start:id_end].strip()

            # Check if it's a site ID (8+ digits starting with 1 or 3)
            if len(site_id) >= 8 and re.match(r'^[13]\d{8,}$', site_id):
                chunk = html[idx:idx + 600]

                # Extract name: \"name\":\"...\"
                name_pattern = '\\"name\\":\\"'
                name_idx = chunk.find(name_pattern)
                if name_idx > -1:
                    name_start = name_idx + len(name_pattern)
                    name_end = chunk.find('\\"', name_start)
                    if name_end > -1:
                        name = chunk[name_start:name_end]

                        # Extract lat: \"lat\":
                        lat_pattern = '\\"lat\\":'
                        lat_idx = chunk.find(lat_pattern)
                        if lat_idx > -1:
                            lat_start = lat_idx + len(lat_pattern)
                            lat_end_comma = chunk.find(',', lat_start)
                            try:
                                lat = float(chunk[lat_start:lat_end_comma])
                            except (ValueError, TypeError):
                                pos = idx + 1
                                continue

                            # Extract lon: \"lon\":
                            lon_pattern = '\\"lon\\":'
                            lon_idx = chunk.find(lon_pattern)
                            if lon_idx > -1:
                                lon_start = lon_idx + len(lon_pattern)
                                lon_end_brace = chunk.find('}', lon_start)
                                try:
                                    lon = float(chunk[lon_start:lon_end_brace])
                                except (ValueError, TypeError):
                                    pos = idx + 1
                                    continue

                                if not (lat == 0.0 and lon == 0.0):
                                    counters.append({
                                        'id': int(site_id),
                                        'name': name,
                                        'lat': lat,
                                        'lon': lon,
                                    })

            pos = idx + 1

        return counters

    async def check_migration_api(self, session: aiohttp.ClientSession, org_id: int) -> tuple[str | None, str]:
        """
        Check if an organization ID has been migrated to a subdomain.

        Returns:
            Tuple of (subdomain URL or None, status message)
        """
        await self._check_migration_backoff()

        try:
            async with session.get(self.MIGRATION_API_URL, params={'domainId': org_id}) as response:
                if response.status == 429:
                    self._trigger_migration_backoff()
                    return None, "migration rate limited"

                if response.status in (404, 403, 401):
                    self._reset_migration_backoff()
                    return None, "no migration"

                if response.status >= 500:
                    return None, f"migration server error ({response.status})"

                data = await response.json()
                self._reset_migration_backoff()

                if not data or not isinstance(data, list) or len(data) == 0:
                    return None, "no migration"

                # Extract subdomain URL from first page entry
                first_page = data[0]
                subdomain_url = first_page.get('url') or first_page.get('domain')
                if subdomain_url:
                    return subdomain_url, "migrated"

                return None, "no migration"

        except asyncio.TimeoutError:
            return None, "migration timeout"
        except aiohttp.ClientError as e:
            self.stats['errors'] += 1
            return None, f"migration error: {type(e).__name__}"
        except Exception as e:
            self.stats['errors'] += 1
            return None, f"migration error: {e}"

    async def process_id(self, session: aiohttp.ClientSession, org_id: int, discovered_ids: set):
        """Process a single organization ID with dual-API scanning."""
        if org_id in discovered_ids:
            if self.verbose:
                async with self.lock:
                    print(f"[{org_id:>6}] skipped (already discovered)")
            return

        async with self.lock:
            self.stats['checked'] += 1

        # Fire both API calls concurrently
        if self.check_migration:
            legacy_result, migration_result = await asyncio.gather(
                self.check_organization(session, org_id),
                self.check_migration_api(session, org_id),
            )
            org_info, legacy_status = legacy_result
            subdomain_url, migration_status = migration_result
        else:
            org_info, legacy_status = await self.check_organization(session, org_id)
            subdomain_url = None
            migration_status = "skipped"

        has_legacy = org_info is not None
        has_migration = subdomain_url is not None

        # For migrated-only orgs, fetch counters from subdomain HTML before acquiring lock
        migrated_counters = None
        if not has_legacy and has_migration:
            migrated_counters = await self.fetch_counters_from_subdomain(session, subdomain_url)

        async with self.lock:
            if has_legacy and has_migration:
                # Dual-mode: available on both APIs
                tier = "dual"
                org_info['subdomain_url'] = subdomain_url
                org_info['tier'] = tier
                self.stats['found'] += 1
                self.stats['dual_mode'] += 1
                self.results['organizations'].append(org_info)

                if self.verbose:
                    print(f"[{org_id:>6}] ✓ DUAL: {org_info['name']} ({org_info['country']}) - {org_info['counter_count']} counters + {subdomain_url}")
                else:
                    print(f"  ✓ Found [{org_id}]: {org_info['name']} ({org_info['country']}) - {org_info['counter_count']} counters [DUAL]")

            elif has_legacy and not has_migration:
                # Legacy-only: old API only
                tier = "legacy"
                org_info['subdomain_url'] = None
                org_info['tier'] = tier
                self.stats['found'] += 1
                self.stats['legacy_only'] += 1
                self.results['organizations'].append(org_info)

                if self.verbose:
                    print(f"[{org_id:>6}] ✓ LEGACY: {org_info['name']} ({org_info['country']}) - {org_info['counter_count']} counters")
                else:
                    print(f"  ✓ Found [{org_id}]: {org_info['name']} ({org_info['country']}) - {org_info['counter_count']} counters [LEGACY]")

            elif not has_legacy and has_migration:
                # Migrated-only: only on new subdomain
                tier = "migrated"
                tenant_name = self._extract_tenant_name(subdomain_url)

                # Infer country from counter coordinates
                inferred_country = self._infer_country_from_coords(migrated_counters) if migrated_counters else None
                country = inferred_country or 'unknown'

                # Apply country filter (allow 'unknown' = no coords, filter 'other' = coords outside all bboxes)
                if self.country_filter and country not in (self.country_filter, 'unknown'):
                    self.stats['filtered'] += 1
                    if self.verbose:
                        print(f"[{org_id:>6}] filtered ({country}: {tenant_name})")
                    return

                migrated_info = {
                    'id': org_id,
                    'name': tenant_name,
                    'country': country,
                    'counter_count': len(migrated_counters) if migrated_counters else None,
                    'counters': migrated_counters if migrated_counters else [],
                    'subdomain_url': subdomain_url,
                    'tier': tier,
                    'discovered_at': datetime.now().isoformat(),
                }
                self.stats['found'] += 1
                self.stats['migrated_only'] += 1
                self.results['organizations'].append(migrated_info)

                counter_str = f"{len(migrated_counters)} counters" if migrated_counters else "no counters found"
                country_str = f", {country}" if country != 'unknown' else ""
                if self.verbose:
                    print(f"[{org_id:>6}] ✓ MIGRATED: {tenant_name} ({country}) -> {subdomain_url} ({counter_str})")
                else:
                    print(f"  ✓ Found [{org_id}]: {tenant_name} [MIGRATED{country_str}] -> {subdomain_url} ({counter_str})")

            else:
                # Not found on either API
                if "filtered" in legacy_status:
                    self.stats['filtered'] += 1
                if self.verbose:
                    print(f"[{org_id:>6}] {legacy_status}")

    async def explore(self, start_id: int, end_id: int, resume: bool = False):
        """
        Explore a range of organization IDs with concurrent requests.

        Args:
            start_id: First ID to check
            end_id: Last ID to check
            resume: If True, resume from last progress
        """
        # Resume from progress if requested
        if resume:
            last_id = self._load_progress()
            if last_id >= start_id:
                start_id = last_id + 1
                print(f"Resuming from ID {start_id}")

        # Get already discovered IDs to skip
        discovered_ids = {org['id'] for org in self.results.get('organizations', [])}

        country_msg = f" (filtering for country={self.country_filter})" if self.country_filter else ""
        migration_msg = "ON (legacy + migration API)" if self.check_migration else "OFF (legacy only)"
        print(f"\n{'='*60}")
        print(f"Eco-Visio Organization Explorer (Async)")
        print(f"{'='*60}")
        print(f"Exploring IDs {start_id} to {end_id}{country_msg}")
        print(f"Concurrency: {self.concurrency} requests")
        print(f"Migration API: {migration_msg}")
        print(f"Already discovered: {len(discovered_ids)} organizations")
        print(f"Verbose mode: {'ON' if self.verbose else 'OFF'}")
        print(f"{'='*60}\n")

        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded_process(session, org_id):
            async with semaphore:
                await self.process_id(session, org_id, discovered_ids)
                # Small delay between requests
                await asyncio.sleep(self.delay_base + random.uniform(0, 0.3))

        # Double limit since each ID fires 2 requests (legacy + migration), keep limit_per_host unchanged
        total_limit = self.concurrency * 2 if self.check_migration else self.concurrency
        connector = aiohttp.TCPConnector(limit=total_limit, limit_per_host=self.concurrency)

        try:
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
                headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; research bot; cycling-data-research)',
                    'Accept': 'application/json',
                }
            ) as session:
                # Process in batches for progress saving
                batch_size = 100
                ids = list(range(start_id, end_id + 1))

                for batch_start in range(0, len(ids), batch_size):
                    batch = ids[batch_start:batch_start + batch_size]

                    # Create tasks for this batch
                    tasks = [bounded_process(session, org_id) for org_id in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)

                    # Progress update
                    current_id = batch[-1]
                    if not self.verbose:
                        print(f"Progress: checked {self.stats['checked']}, "
                              f"found {self.stats['found']}, "
                              f"filtered {self.stats['filtered']}, "
                              f"current ID: {current_id}")

                    # Save progress and results
                    self._save_progress(current_id)
                    self._save_results()

        except KeyboardInterrupt:
            print("\n\nInterrupted by user. Saving progress...")

        finally:
            # Save final results
            self._save_results()
            if ids:
                self._save_progress(ids[-1] if 'ids' in dir() else start_id)

            # Print summary
            print(f"\n{'='*60}")
            print(f"Exploration Summary")
            print(f"{'='*60}")
            print(f"IDs checked: {self.stats['checked']}")
            print(f"Organizations found: {self.stats['found']}")
            if self.check_migration:
                print(f"  Legacy only: {self.stats['legacy_only']}")
                print(f"  Dual mode:   {self.stats['dual_mode']}")
                print(f"  Migrated:    {self.stats['migrated_only']}")
            print(f"Filtered (wrong country): {self.stats['filtered']}")
            print(f"Errors: {self.stats['errors']}")
            print(f"Rate limited: {self.stats['rate_limited']} times")
            print(f"\nTotal organizations in database: {len(self.results['organizations'])}")
            print(f"Results saved to: {self.RESULTS_FILE}")
            print(f"{'='*60}")

    def save_sqlite(self, db_path: str = "discovered_organizations.db"):
        """Save discovered organizations to SQLite database."""
        orgs = self.results.get('organizations', [])
        if not orgs:
            print("No organizations to save.")
            return

        conn = init_explore_database(db_path)
        cursor = conn.cursor()

        for org in orgs:
            # Insert organization
            cursor.execute("""
                INSERT OR REPLACE INTO organizations (id, name, country, counter_count, discovered_at, subdomain_url, tier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                org['id'],
                org['name'],
                org.get('country', 'unknown'),
                org.get('counter_count', 0),
                org.get('discovered_at', datetime.now().isoformat()),
                org.get('subdomain_url'),
                org.get('tier'),
            ))

            # Insert counter previews
            for counter in org.get('counters', []):
                if counter.get('id'):
                    cursor.execute("""
                        INSERT OR REPLACE INTO counter_previews (id, org_id, name, lat, lon)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        counter['id'],
                        org['id'],
                        counter.get('name'),
                        counter.get('lat'),
                        counter.get('lon'),
                    ))

        conn.commit()
        conn.close()
        print(f"SQLite database saved to: {db_path}")

    def list_discovered(self):
        """Print all discovered organizations, optionally filtered by country."""
        orgs = self.results.get('organizations', [])
        if not orgs:
            print("No organizations discovered yet.")
            return

        # Apply country filter if set (but always include "unknown" from migrated orgs)
        if self.country_filter:
            orgs = [o for o in orgs if o.get('country') in (self.country_filter, 'unknown')]

        if not orgs:
            print(f"No organizations found for country '{self.country_filter}'.")
            return

        # Group by country
        by_country = {}
        for org in orgs:
            country = org.get('country', 'unknown')
            if country not in by_country:
                by_country[country] = []
            by_country[country].append(org)

        # Tier label map
        tier_labels = {'legacy': '[L]', 'dual': '[D]', 'migrated': '[M]'}

        print(f"\n{'='*70}")
        print(f"Discovered Organizations ({len(orgs)} total)")
        print(f"  [L]=Legacy  [D]=Dual  [M]=Migrated")
        print(f"{'='*70}\n")

        for country in sorted(by_country.keys()):
            country_orgs = by_country[country]
            print(f"\n{country.upper()} ({len(country_orgs)} organizations):")
            print("-" * 60)
            for org in sorted(country_orgs, key=lambda x: x['name']):
                tier = org.get('tier', 'unknown')
                label = tier_labels.get(tier, '[?]')
                counters = org.get('counter_count')
                counter_str = f"{counters} counters" if counters is not None else "n/a"
                subdomain = org.get('subdomain_url', '')
                subdomain_str = f" -> {subdomain}" if subdomain else ""
                print(f"  {org['id']:>6} | {label} {org['name'][:38]:<38} | {counter_str:<12}{subdomain_str}")


def main():
    parser = argparse.ArgumentParser(
        description='Explore Eco-Visio API to discover organizations with counters (async version)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Explore IDs 4000-8000, German counters only (recommended range)
  uv run python explore_orgs.py --start 4000 --end 8000

  # Faster exploration with more concurrency
  uv run python explore_orgs.py --start 4000 --end 8000 --concurrency 10

  # Resume interrupted exploration
  uv run python explore_orgs.py --start 1 --end 10000 --resume

  # List already discovered organizations
  uv run python explore_orgs.py --list

  # Verbose mode to see every request
  uv run python explore_orgs.py --start 4000 --end 4100 -v

  # Explore all countries
  uv run python explore_orgs.py --start 1 --end 1000 --all-countries

  # Save to SQLite database
  uv run python explore_orgs.py --start 4000 --end 5000 --sqlite

  # Legacy-only scan (skip migration API)
  uv run python explore_orgs.py --start 4000 --end 5000 --no-migration

Safety features:
  - Exponential backoff on rate limiting (429 responses)
  - Automatic retry with increasing delays
  - Progress saved every 100 IDs (use --resume to continue)
  - Ctrl+C saves progress before exit
        """
    )

    parser.add_argument('--start', type=int, default=1,
                        help='First organization ID to check (default: 1)')
    parser.add_argument('--end', type=int, default=100,
                        help='Last organization ID to check (default: 100)')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from last progress')
    parser.add_argument('--list', action='store_true',
                        help='List discovered organizations and exit')
    parser.add_argument('--all-countries', action='store_true',
                        help='Include all countries (default: Germany only)')
    parser.add_argument('--country', type=str, default='de',
                        help='Country code to filter (default: de)')
    parser.add_argument('--concurrency', type=int, default=5,
                        help='Max concurrent requests (default: 5, max recommended: 10)')
    parser.add_argument('--delay', type=float, default=0.5,
                        help='Base delay between requests in seconds (default: 0.5)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output - show status for every ID checked')
    parser.add_argument('--sqlite', nargs='?', const='discovered_organizations.db',
                        help='Save to SQLite database (optionally specify filename)')
    parser.add_argument('--no-migration', action='store_true',
                        help='Skip migration API checks (legacy-only scanning)')

    args = parser.parse_args()

    # Determine country filter
    country_filter = None if args.all_countries else args.country

    # Warn about high concurrency
    if args.concurrency > 10:
        print(f"⚠️  Warning: High concurrency ({args.concurrency}) may trigger rate limiting!")

    explorer = EcoVisioExplorer(
        concurrency=args.concurrency,
        delay_base=args.delay,
        country_filter=country_filter,
        verbose=args.verbose,
        check_migration=not args.no_migration,
    )

    if args.list:
        explorer.list_discovered()
    else:
        asyncio.run(explorer.explore(args.start, args.end, resume=args.resume))

        # Save to SQLite if requested
        if args.sqlite:
            explorer.save_sqlite(args.sqlite)


if __name__ == "__main__":
    main()
