#!/usr/bin/env python3
"""
Enhanced Logtime Tracker for 42 School v3.0
A professional-grade time tracking tool with comprehensive error handling,
logging, caching, and analytics.

Author: Enhanced by Claude
License: MIT
"""

import os
import sys
import time
import json
import logging
import subprocess
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Tuple, Any
from enum import Enum
import argparse

# Suppress SSL warnings only if explicitly disabled
import urllib3


class ExitCode(Enum):
    """Standard exit codes for the application"""
    SUCCESS = 0
    API_ERROR = 1
    CONFIG_ERROR = 2
    AUTH_ERROR = 3
    NETWORK_ERROR = 4


@dataclass
class Config:
    """Application configuration with environment variable support"""
    api_url: str
    target_hours: float
    request_timeout: int
    max_retries: int
    retry_delay: float
    cache_duration: int
    verify_ssl: bool
    log_level: str
    
    HEADERS = {
        'Content-Type': 'application/json',
        'User-Agent': 'LogTimeTracker/3.0'
    }
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Load configuration from environment variables with defaults"""
        return cls(
            api_url=os.getenv('LOGTIME_API_URL', 'https://logtime-med.1337.ma/api/get_log'),
            target_hours=float(os.getenv('LOGTIME_TARGET_HOURS', '120')),
            request_timeout=int(os.getenv('LOGTIME_TIMEOUT', '15')),
            max_retries=int(os.getenv('LOGTIME_MAX_RETRIES', '3')),
            retry_delay=float(os.getenv('LOGTIME_RETRY_DELAY', '1.5')),
            cache_duration=int(os.getenv('LOGTIME_CACHE_DURATION', '300')),
            verify_ssl=os.getenv('LOGTIME_VERIFY_SSL', 'true').lower() == 'true',
            log_level=os.getenv('LOGTIME_LOG_LEVEL', 'INFO')
        )
    
    @property
    def cache_file(self) -> Path:
        """Get cache file path"""
        return Path.home() / '.logtime_cache.json'
    
    @property
    def log_file(self) -> Path:
        """Get log file path"""
        return Path.home() / '.logtime.log'


@dataclass
class Colors:
    """ANSI color codes for terminal output"""
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ORANGE = '\033[38;5;208m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    RESET = '\033[0m'
    
    @classmethod
    def disable(cls):
        """Disable colors for non-TTY output"""
        for attr in dir(cls):
            if not attr.startswith('_') and attr.isupper():
                setattr(cls, attr, '')


@dataclass
class CacheEntry:
    """Represents a cached API response"""
    data: Dict[str, Any]
    timestamp: float
    key: str
    
    def is_valid(self, duration: int) -> bool:
        """Check if cache entry is still valid"""
        return (time.time() - self.timestamp) < duration


class LogTimeError(Exception):
    """Base exception for LogTime errors"""
    pass


class APIError(LogTimeError):
    """API-related errors"""
    pass


class NetworkError(LogTimeError):
    """Network-related errors"""
    pass


class CacheManager:
    """Manages persistent caching of API responses"""
    
    def __init__(self, cache_file: Path, duration: int):
        self.cache_file = cache_file
        self.duration = duration
        self.cache: Dict[str, CacheEntry] = {}
        self.logger = logging.getLogger(__name__)
        self._load_cache()
    
    def _load_cache(self) -> None:
        """Load cache from disk"""
        if not self.cache_file.exists():
            self.logger.debug("No cache file found")
            return
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                raw_cache = json.load(f)
            
            for key, value in raw_cache.items():
                entry = CacheEntry(**value)
                if entry.is_valid(self.duration):
                    self.cache[key] = entry
                else:
                    self.logger.debug(f"Expired cache entry: {key}")
            
            self.logger.info(f"Loaded {len(self.cache)} valid cache entries")
        except json.JSONDecodeError as e:
            self.logger.error(f"Cache file corrupted: {e}")
            self.cache = {}
        except Exception as e:
            self.logger.error(f"Error loading cache: {e}")
            self.cache = {}
    
    def _save_cache(self) -> None:
        """Persist cache to disk"""
        try:
            raw_cache = {
                k: asdict(v) for k, v in self.cache.items()
                if v.is_valid(self.duration)
            }
            
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(raw_cache, f, indent=2)
            
            self.logger.debug(f"Saved {len(raw_cache)} cache entries")
        except IOError as e:
            self.logger.error(f"Failed to save cache: {e}")
    
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached data if valid"""
        entry = self.cache.get(key)
        if entry and entry.is_valid(self.duration):
            self.logger.debug(f"Cache hit: {key}")
            return entry.data
        
        if entry:
            self.logger.debug(f"Cache expired: {key}")
            del self.cache[key]
        
        return None
    
    def set(self, key: str, data: Dict[str, Any]) -> None:
        """Store data in cache"""
        self.cache[key] = CacheEntry(
            data=data,
            timestamp=time.time(),
            key=key
        )
        self._save_cache()
        self.logger.debug(f"Cache set: {key}")
    
    def clear(self) -> int:
        """Clear all cache entries"""
        count = len(self.cache)
        self.cache.clear()
        
        if self.cache_file.exists():
            try:
                self.cache_file.unlink()
                self.logger.info("Cache file deleted")
            except OSError as e:
                self.logger.error(f"Failed to delete cache file: {e}")
        
        return count


class SessionTracker:
    """Tracks active login sessions on the system"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.sessions: List[datetime] = []
        self._detect_sessions()
    
    def _detect_sessions(self) -> None:
        """Detect active sessions using 'who' command"""
        try:
            result = subprocess.run(
                ['who'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False
            )
            
            if result.returncode != 0:
                self.logger.warning(f"'who' command failed: {result.stderr}")
                return
            
            self.sessions = self._parse_who_output(result.stdout)
            self.logger.info(f"Detected {len(self.sessions)} active sessions")
            
        except subprocess.TimeoutExpired:
            self.logger.error("Session detection timed out")
        except FileNotFoundError:
            self.logger.warning("'who' command not found")
        except Exception as e:
            self.logger.error(f"Session detection error: {e}")
    
    def _parse_who_output(self, output: str) -> List[datetime]:
        """Parse 'who' command output to extract login times"""
        sessions = []
        current_year = datetime.now().year
        
        for line in output.strip().splitlines():
            if not line.strip():
                continue
            
            try:
                parts = line.split()
                if len(parts) < 5:
                    continue
                
                # Format: user tty date time
                month, day, time_str = parts[2], int(parts[3]), parts[4]
                
                # Try current year first, then previous year
                for year in [current_year, current_year - 1]:
                    try:
                        login_dt = datetime.strptime(
                            f"{month} {day} {year} {time_str}",
                            "%b %d %Y %H:%M"
                        )
                        
                        # Validate the date isn't in the future
                        if login_dt <= datetime.now():
                            sessions.append(login_dt)
                            break
                    except ValueError:
                        continue
                        
            except (ValueError, IndexError) as e:
                self.logger.debug(f"Failed to parse session line: {line} - {e}")
                continue
        
        return sessions
    
    def get_total_hours(self) -> float:
        """
        Calculate total session time in hours from the oldest (first) login.
        This represents actual work time since first login, not sum of all terminals.
        """
        if not self.sessions:
            return 0.0
        
        # Use only the oldest session (first login)
        oldest_session = min(self.sessions)
        now = datetime.now()
        
        duration_seconds = max(0, (now - oldest_session).total_seconds())
        return duration_seconds / 3600.0
    
    def get_oldest_session(self) -> Optional[datetime]:
        """Get the oldest active session"""
        return min(self.sessions) if self.sessions else None


class LogTimeAPI:
    """Handles all API interactions"""
    def __init__(self, config: Config, cache_manager: CacheManager):
        self.config = config
        self.cache = cache_manager
        self.logger = logging.getLogger(__name__)
        # Configure SSL warnings
        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.logger.warning("SSL verification disabled")

    def fetch_hours(
        self,
        username: str,
        start_date: date,
        end_date: date,
        use_cache: bool = True
    ) -> float:
        """Fetch logged hours for a date range"""
        # Validate date range
        if start_date > end_date:
            raise ValueError(f"Invalid date range: {start_date} > {end_date}")
        # Check cache first
        cache_key = f"{username}_{start_date}_{end_date}"
        if use_cache:
            cached_data = self.cache.get(cache_key)
            if cached_data is not None:
                return self._extract_hours(cached_data)
        # Make API request
        data = self._make_request(username, start_date, end_date)
        # Cache the response
        if data:
            self.cache.set(cache_key, data)
        return self._extract_hours(data)

    def _make_request(
        self,
        username: str,
        start_date: date,
        end_date: date
    ) -> Optional[Dict[str, Any]]:
        """Make HTTP request to the API with retries"""
        
        payload = {
            "login": username,
            "startDate": f"{start_date}T00:00:00.000Z",
            "endDate": f"{end_date}T23:59:59.999Z"
        }
        
        last_error = None
        
        for attempt in range(1, self.config.max_retries + 1):
            try:
                self.logger.debug(
                    f"API request attempt {attempt}/{self.config.max_retries}"
                )
                
                response = requests.post(
                    self.config.api_url,
                    headers=self.config.HEADERS,
                    json=payload,
                    timeout=self.config.request_timeout,
                    verify=self.config.verify_ssl
                )
                
                response.raise_for_status()
                data = response.json()
                
                self.logger.info(
                    f"API request successful for {username} "
                    f"({start_date} to {end_date})"
                )
                
                return data
                
            except requests.exceptions.Timeout as e:
                last_error = NetworkError(f"Request timed out: {e}")
                self.logger.warning(f"Attempt {attempt} timed out")
                
            except requests.exceptions.ConnectionError as e:
                last_error = NetworkError(f"Connection failed: {e}")
                self.logger.warning(f"Attempt {attempt} connection failed")
                
            except requests.exceptions.HTTPError as e:
                if response.status_code == 401:
                    raise APIError(f"Authentication failed for user: {username}")
                elif response.status_code == 404:
                    raise APIError(f"User not found: {username}")
                else:
                    last_error = APIError(f"HTTP {response.status_code}: {e}")
                    self.logger.warning(f"Attempt {attempt} HTTP error")
                    
            except requests.exceptions.RequestException as e:
                last_error = NetworkError(f"Request failed: {e}")
                self.logger.warning(f"Attempt {attempt} failed")
            
            # Wait before retry (except on last attempt)
            if attempt < self.config.max_retries:
                time.sleep(self.config.retry_delay * attempt)
        
        # All retries exhausted
        self.logger.error(f"All {self.config.max_retries} attempts failed")
        if last_error:
            raise last_error
        
        return None
    
    def _extract_hours(self, data: Optional[Dict[str, Any]]) -> float:
        """Extract total hours from API response"""
        if not data:
            return 0.0
        
        try:
            members = data.get('hydra:member', [])
            if not members:
                self.logger.debug("No members found in API response")
                return 0.0
            
            hours = float(members[0].get('totalHours', 0))
            return max(0.0, hours)
            
        except (KeyError, ValueError, TypeError, IndexError) as e:
            self.logger.error(f"Failed to extract hours from response: {e}")
            return 0.0


class PeriodCalculator:
    """Calculates billing periods (28th to 27th)"""
    
    @staticmethod
    def get_current_period(reference_date: date) -> Tuple[date, date]:
        """
        Calculate the current billing period based on reference date.
        Period runs from 28th of one month to 27th of next month.
        """
        if reference_date.day >= 28:
            # We're in the period that started on the 28th of this month
            start = date(reference_date.year, reference_date.month, 28)
            
            # Calculate next month
            if reference_date.month == 12:
                end = date(reference_date.year + 1, 1, 27)
            else:
                end = date(reference_date.year, reference_date.month + 1, 27)
        else:
            # We're in the period that started last month
            if reference_date.month == 1:
                start = date(reference_date.year - 1, 12, 28)
            else:
                start = date(reference_date.year, reference_date.month - 1, 28)
            
            end = date(reference_date.year, reference_date.month, 27)
        
        return start, end
    
    @staticmethod
    def get_time_remaining(end_date: date) -> Tuple[int, int, int]:
        """
        Calculate time remaining until period end.
        Returns: (days, hours, minutes)
        """
        end_datetime = datetime.combine(end_date, datetime.max.time())
        now = datetime.now()
        
        if now >= end_datetime:
            return 0, 0, 0
        
        delta = end_datetime - now
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        return days, hours, minutes


class StatsFormatter:
    """Formats and displays statistics"""
    
    def __init__(self, colors: Colors):
        self.colors = colors
    
    def format_time(self, hours: float) -> str:
        """Format hours as 'Xh Ym'"""
        hours = max(0, hours)
        h = int(hours)
        m = int(round((hours - h) * 60))
        return f"{h}h {m:02d}m"
    
    def progress_bar(
        self,
        current: float,
        target: float,
        width: int = 50
    ) -> str:
        """Generate a colored progress bar"""
        if target <= 0:
            percent = 100.0
        else:
            percent = min(100.0, (current / target) * 100)
        
        filled = int(percent * width / 100)
        bar = "█" * filled + "░" * (width - filled)
        
        # Color based on progress
        if percent >= 100:
            color = self.colors.GREEN
        elif percent >= 75:
            color = self.colors.YELLOW
        elif percent >= 50:
            color = self.colors.ORANGE
        else:
            color = self.colors.RED
        
        return f"{color}[{bar}]{self.colors.RESET} {percent:.1f}%"
    
    def status_message(
        self,
        required_daily: float,
        days_left: int
    ) -> str:
        """Generate status message based on required daily hours"""
        if required_daily <= 0:
            return (
                f"{self.colors.GREEN}{self.colors.BOLD}"
                f"🎯 Congratulations! Target achieved!"
                f"{self.colors.RESET}"
            )
        elif days_left <= 0:
            return (
                f"{self.colors.RED}{self.colors.BOLD}"
                f"⏰ Period has ended!"
                f"{self.colors.RESET}"
            )
        elif required_daily <= 4:
            return (
                f"{self.colors.GREEN}✅ Excellent pace: "
                f"{self.format_time(required_daily)}/day needed"
                f"{self.colors.RESET}"
            )
        elif required_daily <= 6:
            return (
                f"{self.colors.YELLOW}📈 Good pace: "
                f"{self.format_time(required_daily)}/day needed"
                f"{self.colors.RESET}"
            )
        elif required_daily <= 8:
            return (
                f"{self.colors.ORANGE}⚡ Needs attention: "
                f"{self.format_time(required_daily)}/day needed"
                f"{self.colors.RESET}"
            )
        else:
            return (
                f"{self.colors.RED}{self.colors.BOLD}"
                f"🔥 CRITICAL: {self.format_time(required_daily)}/day needed!"
                f"{self.colors.RESET}"
            )


class LogTimeTracker:
    """Main application class"""
    
    def __init__(self, config: Config, username: Optional[str] = None):
        self.config = config
        self.today = date.today()
        self.colors = Colors()
        self.logger = logging.getLogger(__name__)
        self.username = username or self._detect_username()
        
        # Initialize components
        self.cache_manager = CacheManager(
            config.cache_file,
            config.cache_duration
        )
        self.api = LogTimeAPI(config, self.cache_manager)
        self.session_tracker = SessionTracker()
        self.formatter = StatsFormatter(self.colors)
    
    def _detect_username(self) -> str:
        """Detect current system username"""
        methods = [
            lambda: os.getenv('USER'),
            lambda: os.getenv('LOGNAME'),
            lambda: os.getenv('USERNAME'),
            lambda: subprocess.check_output(['whoami'], text=True).strip(),
            lambda: subprocess.check_output(['id', '-un'], text=True).strip(),
        ]
        
        for method in methods:
            try:
                username = method()
                if username and username.strip():
                    self.logger.debug(f"Username detected: {username}")
                    return username.strip()
            except Exception as e:
                self.logger.debug(f"Username detection method failed: {e}")
                continue
        
        self.logger.warning("Could not detect username")
        return "unknown_user"
    
    def run(self, no_cache: bool = False, export_json: bool = False) -> int:
        """Main execution method"""
        try:
            # Calculate period
            start_date, end_date = PeriodCalculator.get_current_period(self.today)
            
            # Fetch data
            self.logger.info(f"Fetching data for user: {self.username}")
            
            today_hours = self.api.fetch_hours(
                self.username,
                self.today,
                self.today,
                use_cache=not no_cache
            )
            
            period_hours = self.api.fetch_hours(
                self.username,
                start_date,
                end_date,
                use_cache=not no_cache
            )
            
            # Get session time
            session_hours = self.session_tracker.get_total_hours()
            
            # Calculate metrics
            days_left, hours_left, mins_left = PeriodCalculator.get_time_remaining(
                end_date
            )
            
            hours_remaining = max(0, self.config.target_hours - period_hours)
            total_time_left_hours = days_left * 24 + hours_left + mins_left / 60
            
            # Calculate required daily including current session
            projected_hours = period_hours + session_hours
            projected_remaining = max(0, self.config.target_hours - projected_hours)
            
            required_daily = (
                projected_remaining / (total_time_left_hours / 24)
                if total_time_left_hours > 0
                else 0
            )
            
            # Display output
            if export_json:
                self._export_json({
                    'username': self.username,
                    'date': self.today.isoformat(),
                    'period': {
                        'start': start_date.isoformat(),
                        'end': end_date.isoformat()
                    },
                    'hours': {
                        'today': today_hours,
                        'session': session_hours,
                        'period_total': period_hours,
                        'projected': period_hours + session_hours,
                        'target': self.config.target_hours,
                        'remaining': hours_remaining,
                        'required_daily': required_daily
                    },
                    'time_remaining': {
                        'days': days_left,
                        'hours': hours_left,
                        'minutes': mins_left
                    }
                })
            else:
                self._display_report(
                    start_date, end_date,
                    today_hours, session_hours, period_hours,
                    hours_remaining, required_daily,
                    days_left, hours_left, mins_left
                )
            
            return ExitCode.SUCCESS.value
            
        except APIError as e:
            self.logger.error(f"API error: {e}")
            print(f"{self.colors.RED}Error: {e}{self.colors.RESET}", file=sys.stderr)
            return ExitCode.API_ERROR.value
            
        except NetworkError as e:
            self.logger.error(f"Network error: {e}")
            print(
                f"{self.colors.RED}Network error: {e}{self.colors.RESET}",
                file=sys.stderr
            )
            return ExitCode.NETWORK_ERROR.value
            
        except Exception as e:
            self.logger.exception("Unexpected error")
            print(
                f"{self.colors.RED}Unexpected error: {e}{self.colors.RESET}",
                file=sys.stderr
            )
            return ExitCode.API_ERROR.value
    
    def _display_report(
        self,
        start_date: date,
        end_date: date,
        today_hours: float,
        session_hours: float,
        period_hours: float,
        hours_remaining: float,
        required_daily: float,
        days_left: int,
        hours_left: int,
        mins_left: int
    ) -> None:
        """Display formatted report to console"""
        c = self.colors
        f = self.formatter
        
        # Calculate totals
        today_total = today_hours + session_hours
        projected_total = period_hours + session_hours
        
        # Header with branding
        print(f"\n{c.PURPLE}{c.BOLD}╔{'═'*68}╗{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}║{' '*68}║{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}║{' '*20}🕰️  LOGTIME TRACKER{' '*29}║{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}║{' '*24}42 School{' '*36}║{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}║{' '*68}║{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}╚{'═'*68}╝{c.RESET}\n")
        
        # User Info Card
        print(f"{c.CYAN}┌─ USER INFORMATION {c.RESET}{'─'*48}")
        print(f"{c.CYAN}│{c.RESET} {c.BOLD}Username:{c.RESET} {self.username}")
        print(f"{c.CYAN}│{c.RESET} {c.BOLD}Date:{c.RESET}     {self.today.strftime('%A, %B %d, %Y')}")
        print(f"{c.CYAN}│{c.RESET} {c.BOLD}Period:{c.RESET}   {start_date.strftime('%b %d')} → {end_date.strftime('%b %d, %Y')}")
        print(f"{c.CYAN}└{'─'*68}{c.RESET}\n")
        
        # Today's Activity Section
        print(f"{c.BOLD}📅 TODAY'S ACTIVITY{c.RESET}")
        print(f"{'─'*70}")
        
        # Logged hours (completed sessions)
        print(f"{c.GREEN}✓{c.RESET} Previously logged today    : {c.BOLD}{f.format_time(today_hours)}{c.RESET}")
        
        # Current session (active)
        if session_hours > 0:
            oldest = self.session_tracker.get_oldest_session()
            session_start = oldest.strftime('%H:%M') if oldest else 'unknown'
            print(f"{c.YELLOW}⏳{c.RESET} Current session (active)   : {c.BOLD}{f.format_time(session_hours)}{c.RESET} {c.CYAN}(since {session_start}){c.RESET}")
        else:
            print(f"{c.YELLOW}⏳{c.RESET} Current session (active)   : {c.BOLD}0h 00m{c.RESET}")
        
        # Today's total
        print(f"{'─'*70}")
        print(f"{c.BOLD}📊 Today's total (projected)  : {f.format_time(today_total)}{c.RESET}")
        print()
        
        # Period Overview Section
        print(f"{c.BOLD}📈 PERIOD OVERVIEW{c.RESET}")
        print(f"{'─'*70}")
        print(f"Logged hours so far         : {c.BOLD}{f.format_time(period_hours)}{c.RESET}")
        print(f"With current session        : {c.BOLD}{f.format_time(projected_total)}{c.RESET}")
        print(f"Target for period           : {c.BOLD}{f.format_time(self.config.target_hours)}{c.RESET}")
        
        # Calculate remaining based on projection
        projected_remaining = max(0, self.config.target_hours - projected_total)
        print(f"Remaining to reach target   : {c.BOLD}{f.format_time(projected_remaining)}{c.RESET}")
        print()
        
        # Progress Bar
        print(f"{c.BOLD}Progress{c.RESET}")
        print(f.progress_bar(projected_total, self.config.target_hours))
        print()
        
        # Time Analysis Section
        print(f"{c.BOLD}⏱️  TIME ANALYSIS{c.RESET}")
        print(f"{'─'*70}")
        print(f"Days remaining in period    : {c.BOLD}{days_left}d {hours_left}h {mins_left}m{c.RESET}")
        print(f"Required daily average      : {c.BOLD}{f.format_time(required_daily)}{c.RESET}")
        
        # Pace indicator
        if required_daily > 0 and days_left > 0:
            days_elapsed = max(1, (datetime.now() - datetime.combine(start_date, datetime.min.time())).days)
            pace_per_day = projected_total / days_elapsed
            
            if pace_per_day >= required_daily:
                pace_status = f"{c.GREEN}✓ On track{c.RESET}"
            elif pace_per_day >= required_daily * 0.85:
                pace_status = f"{c.YELLOW}△ Slightly behind{c.RESET}"
            else:
                pace_status = f"{c.RED}✗ Behind schedule{c.RESET}"
            print(f"Current daily pace          : {c.BOLD}{f.format_time(pace_per_day)}{c.RESET} {pace_status}")
        print()
        
        # Status Message
        print(f"{c.BOLD}💡 STATUS{c.RESET}")
        print(f"{'─'*70}")
        print(f.status_message(required_daily, days_left))
        
        # Motivational footer
        progress_percent = min(100, (projected_total / self.config.target_hours) * 100) if self.config.target_hours > 0 else 100
        
        print(f"\n{c.PURPLE}{c.BOLD}╔{'═'*68}╗{c.RESET}")
        if progress_percent >= 100:
            print(f"{c.PURPLE}{c.BOLD}║{c.GREEN}{c.BOLD}  🎉 TARGET ACHIEVED! Keep up the excellent work! 🎉{' '*16}║{c.RESET}")
        elif progress_percent >= 80:
            print(f"{c.PURPLE}{c.BOLD}║{c.GREEN}  💪 Almost there! You're doing great!{' '*28}║{c.RESET}")
        elif progress_percent >= 60:
            print(f"{c.PURPLE}{c.BOLD}║{c.YELLOW}  📚 Keep going! You're making solid progress!{' '*21}║{c.RESET}")
        else:
            print(f"{c.PURPLE}{c.BOLD}║{c.ORANGE}  ⚡ Time to focus! You can do this!{' '*31}║{c.RESET}")
        print(f"{c.PURPLE}{c.BOLD}╚{'═'*68}╝{c.RESET}\n")
    
    def _export_json(self, data: Dict[str, Any]) -> None:
        """Export data as JSON"""
        print(json.dumps(data, indent=2))


def setup_logging(config: Config) -> None:
    """Configure application logging"""
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_formatter = logging.Formatter(
        '%(levelname)s: %(message)s'
    )

    # File handler
    try:
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
    except IOError:
        file_handler = None

    # Console handler (only for warnings and errors)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(console_formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    if file_handler:
        root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def main() -> int:
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Enhanced Logtime Tracker for 42 School v3.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  LOGTIME_API_URL           API endpoint URL
  LOGTIME_TARGET_HOURS      Target hours (default: 120)
  LOGTIME_TIMEOUT           Request timeout in seconds (default: 15)
  LOGTIME_MAX_RETRIES       Maximum retry attempts (default: 3)
  LOGTIME_RETRY_DELAY       Delay between retries (default: 1.5)
  LOGTIME_CACHE_DURATION    Cache validity in seconds (default: 300)
  LOGTIME_VERIFY_SSL        Verify SSL certificates (default: true)
  LOGTIME_LOG_LEVEL         Logging level (default: INFO)

Examples:
  %(prog)s                          # Check current user
  %(prog)s username                 # Check specific user
  %(prog)s --no-cache               # Bypass cache
  %(prog)s --export-json            # Export as JSON
  %(prog)s --clear-cache            # Clear cache
  %(prog)s --target 100             # Custom target hours
        """
    )
    
    parser.add_argument(
        'username',
        nargs='?',
        help='Username to check (default: current user)'
    )
    
    parser.add_argument(
        '--target',
        type=float,
        metavar='HOURS',
        help='Target hours (overrides environment variable)'
    )
    
    parser.add_argument(
        '--no-cache',
        action='store_true',
        help='Bypass cache and fetch fresh data'
    )
    
    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='Clear cache and exit'
    )
    
    parser.add_argument(
        '--export-json',
        action='store_true',
        help='Export data as JSON'
    )
    
    parser.add_argument(
        '--no-color',
        action='store_true',
        help='Disable colored output'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 3.0'
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config = Config.from_env()
    
    # Override target if specified
    if args.target:
        config.target_hours = args.target
    
    # Set debug mode
    if args.debug:
        config.log_level = 'DEBUG'
    
    # Setup logging
    setup_logging(config)
    logger = logging.getLogger(__name__)
    
    # Disable colors if requested or not a TTY
    if args.no_color or not sys.stdout.isatty():
        Colors.disable()
    
    # Handle cache clearing
    if args.clear_cache:
        cache_manager = CacheManager(config.cache_file, config.cache_duration)
        count = cache_manager.clear()
        print(f"{Colors.GREEN}✓ Cleared {count} cache entries{Colors.RESET}")
        return ExitCode.SUCCESS.value
    
    # Run tracker
    try:
        tracker = LogTimeTracker(config, args.username)
        return tracker.run(
            no_cache=args.no_cache,
            export_json=args.export_json
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted by user{Colors.RESET}")
        return ExitCode.SUCCESS.value
    except Exception as e:
        logger.exception("Fatal error")
        print(f"{Colors.RED}Fatal error: {e}{Colors.RESET}", file=sys.stderr)
        return ExitCode.API_ERROR.value


if __name__ == "__main__":
    sys.exit(main())

