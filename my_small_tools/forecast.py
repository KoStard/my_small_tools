"""
Time Series Forecaster - ESP32 Battery & More
=============================================

A smart CSV forecaster that handles messy data, auto-detects patterns,
and creates interactive visualizations.

Usage:
  # Quick run
  forecast --csv data.csv --values voltage --dates timestamp --forecast 7days

  # Save a reusable copy with your parameters
  forecast --csv data.csv --values voltage --dates timestamp --forecast 7days --save-script my_analysis.py

  # Run the saved copy (uses hardcoded defaults)
  uv run my_analysis.py

  # Override saved parameters
  uv run my_analysis.py --forecast 2weeks --seasonal-period 12

Date column formats handled automatically:
  - "2024-01-15 14:30:00"
  - "2024-01-15"
  - "01/15/2024 2:30 PM"
  - "15-Jan-2024"
  ...and many more
"""

import argparse
import sys
import json
from pathlib import Path
from typing import Optional, Tuple, Any

import pandas as pd
import plotly.graph_objects as go
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from dateutil import parser as dateparser
from dateutil.parser import ParserError


def parse_time_string(time_str: str) -> int:
    """
    Parse flexible time strings like "7days", "2weeks", "1month", "1year"
    Returns: number of days
    """
    time_str = time_str.lower().replace(' ', '')

    multipliers = {
        'day': 1,
        'days': 1,
        'week': 7,
        'weeks': 7,
        'month': 30,
        'months': 30,
        'year': 365,
        'years': 365,
    }

    for unit, multiplier in multipliers.items():
        if time_str.endswith(unit):
            try:
                number = int(time_str[:-len(unit)])
                return number * multiplier
            except ValueError:
                raise ValueError(f"Invalid number in time string: '{time_str}'")

    raise ValueError(f"Invalid time format: '{time_str}'. Use formats like '7days', '2weeks', '1month', '1year'")


def smart_date_parse(date_string: str) -> Optional[pd.Timestamp]:
    """Parse dates intelligently, return None if invalid"""
    try:
        # Try pandas first (fast)
        return pd.to_datetime(date_string)
    except:
        try:
            # Fall back to dateutil (comprehensive)
            parsed = dateparser.parse(str(date_string))
            if parsed is None:
                return None
            return pd.Timestamp(parsed)
        except (ParserError, ValueError, TypeError):
            return None


def detect_seasonal_period(freq: str) -> Optional[int]:
    """
    Auto-detect seasonal period from pandas frequency string
    Returns: seasonal period or None if no clear seasonality
    """
    # Frequency to seasonal period mapping
    freq_map = {
        'h': 24,      # hourly -> daily seasonality
        'D': 7,       # daily -> weekly seasonality
        'W': 52,      # weekly -> yearly seasonality
        'M': 12,      # monthly -> yearly seasonality
    }

    # Extract base frequency (e.g., 'H' from '6H')
    base_freq = freq[0] if freq else ''

    return freq_map.get(base_freq)


def load_and_clean_data(
    csv_path: str,
    values_col: str,
    dates_col: str
) -> Tuple[pd.Series, pd.Series, list]:
    """
    Load CSV, parse dates, convert values, report invalid rows
    Returns: (clean_dates, clean_values, invalid_reports)
    """
    print(f"📂 Loading CSV: {csv_path}")

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        raise ValueError(f"Failed to read CSV: {e}")

    print(f"📊 Found {len(df)} rows, {len(df.columns)} columns")
    print(f"   Columns: {list(df.columns)}")

    # Validate columns exist
    if values_col not in df.columns:
        raise ValueError(f"Values column '{values_col}' not found in CSV")
    if dates_col not in df.columns:
        raise ValueError(f"Dates column '{dates_col}' not found in CSV")

    invalid_reports = []
    valid_indices = []

    # Parse each row
    for idx, row in df.iterrows():
        date_val = row[dates_col]
        value_val = row[values_col]

        # Skip empty rows
        if pd.isna(date_val) or pd.isna(value_val):
            invalid_reports.append({
                'row': idx + 1,
                'reason': 'empty date or value',
                'date': str(date_val),
                'value': str(value_val)
            })
            continue

        # Parse date
        parsed_date = smart_date_parse(date_val)
        if parsed_date is None:
            invalid_reports.append({
                'row': idx + 1,
                'reason': f'invalid date format: {date_val}',
                'date': str(date_val),
                'value': str(value_val)
            })
            continue

        # Parse value
        try:
            parsed_value = float(value_val)
        except (ValueError, TypeError):
            invalid_reports.append({
                'row': idx + 1,
                'reason': f'invalid value: {value_val}',
                'date': str(date_val),
                'value': str(value_val)
            })
            continue

        # All good - store valid data
        df.at[idx, '_parsed_date'] = parsed_date
        df.at[idx, '_parsed_value'] = parsed_value
        valid_indices.append(idx)

    # Extract valid data
    if not valid_indices:
        raise ValueError("No valid rows found in CSV")

    valid_df = df.loc[valid_indices]
    dates = pd.Series([d for d in valid_df['_parsed_date']], index=valid_indices)
    values = pd.Series([v for v in valid_df['_parsed_value']], index=valid_indices)

    # Sort by date
    sorted_indices = dates.argsort()
    dates = dates.iloc[sorted_indices].reset_index(drop=True)
    values = values.iloc[sorted_indices].reset_index(drop=True)

    print(f"✅ {len(valid_indices)} valid rows, {len(invalid_reports)} invalid rows")

    return dates, values, invalid_reports


def create_forecast(
    dates: pd.Series,
    values: pd.Series,
    forecast_days: int,
    seasonal_period: Optional[int] = None
) -> Tuple[pd.Series, pd.Series, str]:
    """
    Create Holt-Winters forecast
    Returns: (historical_series, forecast_series, frequency_info)
    """
    # Create time series
    ts = pd.Series(values.values, index=dates)

    # Resample to regular frequency (handles missing timestamps)
    inferred_freq = pd.infer_freq(ts.index)
    if inferred_freq is None:
        # If no clear frequency, resample to hourly
        print("⚠️  Could not detect regular frequency, resampling to hourly")
        ts_regular = ts.resample('h').mean()
        freq_str = 'h'
    else:
        ts_regular = ts.asfreq(inferred_freq)
        freq_str = inferred_freq

    print(f"📈 Detected frequency: {freq_str}")

    # Count non-NaN values
    non_nan_count = ts_regular.count()
    print(f"   Non-NaN values: {non_nan_count} out of {len(ts_regular)}")

    # Check if we have enough data
    if non_nan_count < 2:
        raise ValueError(f"Not enough valid data points (only {non_nan_count}) to create a forecast")

    # Auto-detect seasonality if not provided
    if seasonal_period is None:
        seasonal_period = detect_seasonal_period(freq_str)
        if seasonal_period:
            print(f"🔍 Auto-detected seasonal period: {seasonal_period}")
        else:
            print("⚠️  Could not auto-detect seasonality, using no seasonality")
    else:
        print(f"📅 Using provided seasonal period: {seasonal_period}")

    # Check if we have enough data for the seasonal period
    if seasonal_period is not None and non_nan_count < 2 * seasonal_period:
        print(f"⚠️  Too few non-NaN values ({non_nan_count}) for seasonal period {seasonal_period}. Disabling seasonality.")
        seasonal_period = None

    # Fill missing values
    if ts_regular.isna().any():
        print("   Filling missing values with forward fill and then backward fill")
        ts_regular = ts_regular.ffill().bfill()
        
        # If there are still NaNs (e.g., at the beginning if no backward fill possible), then fill with mean
        if ts_regular.isna().any():
            print("   Filling remaining NaNs with mean")
            ts_regular = ts_regular.fillna(ts_regular.mean())

    # Prepare model parameters
    model_kwargs = {
        'trend': 'add',
        'initialization_method': 'estimated'
    }

    if seasonal_period:
        model_kwargs['seasonal'] = 'add'
        model_kwargs['seasonal_periods'] = seasonal_period

    # Fit model
    print("🤖 Fitting Holt-Winters model...")
    try:
        model = ExponentialSmoothing(ts_regular, **model_kwargs)
        fitted = model.fit()
    except Exception as e:
        print(f"⚠️  Failed to fit model with seasonality: {e}")
        if seasonal_period is not None:
            print("   Retrying without seasonality")
            model_kwargs.pop('seasonal', None)
            model_kwargs.pop('seasonal_periods', None)
            model = ExponentialSmoothing(ts_regular, **model_kwargs)
            fitted = model.fit()
        else:
            raise

    # Generate forecast
    periods_to_forecast = forecast_days * 24 if freq_str == 'h' else forecast_days
    forecast = fitted.forecast(periods_to_forecast)

    return ts_regular, forecast, freq_str


def create_interactive_plot(
    historical: pd.Series,
    forecast: pd.Series,
    freq_str: str,
    values_col: str,
    csv_path: str
) -> None:
    """
    Create interactive Plotly plot and open in browser
    """
    print("📊 Creating interactive plot...")

    # Prepare data for plot
    hist_df = historical.reset_index()
    hist_df.columns = ['timestamp', values_col]
    hist_df['type'] = 'historical'

    forecast_df = forecast.reset_index()
    forecast_df.columns = ['timestamp', values_col]
    forecast_df['type'] = 'forecast'

    # Combine for plotting
    plot_df = pd.concat([hist_df, forecast_df], ignore_index=True)

    # Create figure
    fig = go.Figure()

    # Historical data
    fig.add_trace(go.Scatter(
        x=hist_df['timestamp'],
        y=hist_df[values_col],
        mode='lines+markers',
        name='Historical',
        line=dict(color='#2E86AB', width=2),
        marker=dict(size=3)
    ))

    # Forecast data
    fig.add_trace(go.Scatter(
        x=forecast_df['timestamp'],
        y=forecast_df[values_col],
        mode='lines+markers',
        name='Forecast',
        line=dict(color='#A23B72', width=2, dash='dash'),
        marker=dict(size=3, symbol='diamond')
    ))

    # Add separator line
    last_historical_date = hist_df['timestamp'].max()
    fig.add_vline(
        x=last_historical_date.isoformat(),
        line=dict(color='gray', width=1, dash='dot')
    )
    
    # Add annotation manually to avoid Plotly's arithmetic issues
    fig.add_annotation(
        x=last_historical_date.isoformat(),
        y=hist_df[values_col].max(),
        text="Forecast Start",
        showarrow=True,
        arrowhead=2,
        arrowsize=1,
        arrowwidth=1,
        arrowcolor="gray",
        ax=40,
        ay=-40
    )

    # Add danger zone for battery voltage (if values look like voltage)
    if values_col.lower() in ['voltage', 'battery', 'vbat', 'volt']:
        fig.add_hline(
            y=3.3,
            line=dict(color='red', width=2, dash='dot'),
            annotation_text="Danger Zone (3.3V)"
        )

    # Layout
    fig.update_layout(
        title=f'Time Series Forecast: {values_col}<br><sub>Source: {csv_path}</sub>',
        xaxis_title='Timestamp',
        yaxis_title=values_col,
        hovermode='x unified',
        template='plotly_white',
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="left",
            x=0.01
        )
    )

    # Show plot in browser
    fig.show()
    print("✅ Plot opened in browser")


def print_invalid_report(invalid_reports: list, max_reports: int = 10) -> None:
    """
    Print report of invalid rows
    """
    if not invalid_reports:
        print("✅ No invalid rows found")
        return

    print(f"\n⚠️  Found {len(invalid_reports)} invalid rows:")
    print(f"   (showing first {min(max_reports, len(invalid_reports))} rows)")
    print("-" * 60)

    for i, report in enumerate(invalid_reports[:max_reports]):
        print(f"Row {report['row']:4d}: {report['reason']}")
        print(f"        Date: {report['date']}")
        print(f"        Value: {report['value']}")

    if len(invalid_reports) > max_reports:
        print(f"\n... and {len(invalid_reports) - max_reports} more invalid rows")

    print("-" * 60)


def generate_script(
    csv_path: str,
    values_col: str,
    dates_col: str,
    forecast_days: int,
    seasonal_period: Optional[int],
    output_path: str
) -> None:
    """
    Generate a self-contained script with hardcoded parameters
    """
    script_content = f'''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pandas>=2.0",
#   "statsmodels>=0.14",
#   "plotly>=5.0",
#   "python-dateutil>=2.8",
# ]
# ///

"""
Auto-generated forecast script
================================
Source: {csv_path}
Values column: {values_col}
Dates column: {dates_col}
Forecast days: {forecast_days}
Seasonal period: {seasonal_period}

Usage:
  # Run with default (hardcoded) parameters
  uv run {Path(output_path).name}

  # Override parameters
  uv run {Path(output_path).name} --forecast 14days --seasonal-period 12
"""

import argparse
import pandas as pd
import plotly.graph_objects as go
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from dateutil import parser as dateparser
from dateutil.parser import ParserError

# DEFAULTS - edit these if you want to change hardcoded values
DEFAULT_CSV = r'{csv_path}'
DEFAULT_VALUES_COL = '{values_col}'
DEFAULT_DATES_COL = '{dates_col}'
DEFAULT_FORECAST_DAYS = {forecast_days}
DEFAULT_SEASONAL_PERIOD = {seasonal_period if seasonal_period else 'None'}

{'# ' + chr(10) + '# '.join(open(__file__).read().split(chr(10))[20:])}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Time series forecaster')
    parser.add_argument('--csv', type=str, default=DEFAULT_CSV, help='Path to CSV file')
    parser.add_argument('--values', type=str, default=DEFAULT_VALUES_COL, help='Column name for values')
    parser.add_argument('--dates', type=str, default=DEFAULT_DATES_COL, help='Column name for dates')
    parser.add_argument('--forecast', type=str, default='{{DEFAULT_FORECAST_DAYS}}days', help='Forecast period (e.g., 7days, 2weeks, 1month)')
    parser.add_argument('--seasonal-period', type=int, default=DEFAULT_SEASONAL_PERIOD, help='Seasonal period (auto-detect if None)')

    args = parser.parse_args()

    # Parse forecast days
    forecast_days = parse_time_string(args.forecast)

    # Load and clean data
    dates, values, invalid_reports = load_and_clean_data(
        args.csv,
        args.values,
        args.dates
    )

    # Print invalid reports
    print_invalid_report(invalid_reports)

    # Create forecast
    historical, forecast, freq_str = create_forecast(
        dates,
        values,
        forecast_days,
        args.seasonal_period
    )

    # Create interactive plot
    create_interactive_plot(
        historical,
        forecast,
        freq_str,
        args.values,
        args.csv
    )
'''

    # Write the script
    with open(output_path, 'w') as f:
        f.write(script_content)

    print(f"✅ Script generated successfully")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Time Series Forecaster - Extrapolate CSV data with Holt-Winters',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  forecast --csv homeassistant_export.csv --values voltage --dates timestamp --forecast 7days

  # Save a reusable copy with hardcoded defaults
  forecast --csv homeassistant_export.csv --values voltage --dates timestamp --forecast 7days --save-script my_battery_forecast.py

  # Run the saved copy (uses hardcoded defaults)
  uv run my_battery_forecast.py

  # Override saved defaults for one run
  uv run my_battery_forecast.py --forecast 2weeks --seasonal-period 12

  # Auto-detect seasonality with custom parameters
  uv run forecast.py --csv data.csv --values temperature --dates datetime --forecast 1month

Supported time formats:
  7days, 2weeks, 1month (30 days), 3months, 1year
  (any combination of numbers with 'days', 'weeks', 'months', 'years')
        """
    )

    parser.add_argument('--csv', type=str, required=True, help='Path to CSV file')
    parser.add_argument('--values', type=str, required=True, help='Column name for numeric values')
    parser.add_argument('--dates', type=str, required=True, help='Column name for timestamps')
    parser.add_argument('--forecast', type=str, default='7days', help='Forecast period (e.g., 7days, 2weeks, 1month)')
    parser.add_argument('--seasonal-period', type=int, default=None, help='Seasonal period (auto-detect if omitted, e.g., 24 for hourly data)')
    parser.add_argument('--save-script', type=str, help='Save a self-contained script with these parameters')

    args = parser.parse_args()

    # Parse forecast days
    try:
        forecast_days = parse_time_string(args.forecast)
    except ValueError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

    print(f"🎯 Forecasting {forecast_days} days into the future")

    # Load and clean data
    try:
        dates, values, invalid_reports = load_and_clean_data(
            args.csv,
            args.values,
            args.dates
        )
    except Exception as e:
        print(f"❌ Failed to load CSV: {e}")
        sys.exit(1)

    # Report invalid rows
    if invalid_reports:
        print_invalid_report(invalid_reports)

    # Create forecast
    try:
        historical, forecast, freq_str = create_forecast(
            dates,
            values,
            forecast_days,
            args.seasonal_period
        )
    except Exception as e:
        print(f"❌ Failed to create forecast: {e}")
        sys.exit(1)

    # Create interactive plot
    create_interactive_plot(
        historical,
        forecast,
        freq_str,
        args.values,
        args.csv
    )

    # Save script if requested
    if args.save_script:
        print(f"\n💾 Saving self-contained script to: {args.save_script}")
        try:
            generate_script(
                args.csv,
                args.values,
                args.dates,
                forecast_days,
                args.seasonal_period,
                args.save_script
            )
            print(f"✅ Script saved successfully")
            print(f"   Run it with: uv run {args.save_script}")
        except Exception as e:
            print(f"❌ Failed to save script: {e}")


if __name__ == '__main__':
    main()
