# Time Series Forecast Script - Specification

**Version**: 1.0
**Purpose**: CSV → Interactive forecast plot in 1 command
**Philosophy**: 20/80 quality - Holt-Winters without complexity

***

## Core Features (Implementation Checklist)

- [x] Auto-detect date formats (ISO, US, EU, human-readable)
- [x] Report invalid rows (show row number, reason, values)
- [x] Parse time strings: `7days`, `2weeks`, `1month` (=30 days), `1year`
- [x] Auto-detect seasonality from data frequency
- [x] Holt-Winters forecast (trend + seasonality)
- [x] Interactive Plotly plot (browser) with zoom/pan/hover
- [x] Save reusable script with `--save-script`
- [x] Override saved parameters via CLI
- [x] Handle missing timestamps via resampling
- [x] Danger zone markers for voltage data (3.3V line)
- [ ] Export forecast to CSV
- [ ] Confidence intervals on plot
- [ ] Multiple columns in one run
- [ ] Batch processing multiple CSVs
- [ ] Anomaly detection in historical data
***

## Use Cases

| Use Case | Data Frequency | Seasonal Period | Typical Forecast | Key Question |
|----------|----------------|-----------------|------------------|--------------|
| ESP32 battery voltage | Hourly | 24 (daily) | 7-30 days | "When will it die?" |
| Home temperature | Hourly | 24 (daily) | 2 weeks | "Weekend vs weekday pattern?" |
| Solar generation | Hourly + Daily | 24 or 7 | 7 days | "Next week's production?" |
| Server CPU usage | Hourly | 24 or 7 | 14 days | "When hits 80%?" |
| Monthly spending | Monthly | 12 (yearly) | 3 months | "Budget projection?" |

***

## CLI Specification

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `--csv` | string | Yes | - | Path to CSV file |
| `--values` | string | Yes | - | Column name for numeric values |
| `--dates` | string | Yes | - | Column name for timestamps |
| `--forecast` | string | No | `7days` | Future period (see time formats) |
| `--seasonal-period` | int | No | Auto | Seasonality length (e.g., 24) |
| `--save-script` | string | No | - | Save standalone script path |

### Time Formats
- `Xd` or `Xdays` → X days
- `Xw` or `Xweeks` → X × 7 days
- `Xm` or `Xmonths` → X × 30 days
- `Xy` or `Xyears` → X × 365 days
***

## Input CSV Specification

**Columns**: Exactly 2 columns needed (values + dates)
**Column names**: Any names, specified via CLI
**Date formats**: Any parsable format (auto-detect)
**Value format**: Numeric (int or float)
**Invalid handling**: Report row number, skip, continue
**Missing timestamps**: Filled via resampling, no crash
**Minimum rows**: 10+ (for reliable seasonality detection)

**Example valid rows**:
```csv
timestamp,battery_voltage
2024-01-15 10:00:00,4.12
2024-01-15 11:00:00,4.08
```

**Example invalid rows** (will be reported and skipped):
- Empty cells
- Date: `2024-13-01` (invalid month)
- Value: `N/A` or `null`
- Malformed timestamps
***

## Output Specification

### Console
```
📂 Loading CSV: path/to/file.csv
📊 Found 8760 rows, 3 columns
✅ 8754 valid rows, 6 invalid rows
⚠️  Found 6 invalid rows: [show first 10 with reasons]
📈 Detected frequency: h
🔍 Auto-detected seasonal period: 24
🤖 Fitting Holt-Winters model...
✅ Plot opened in browser
```

### Plot (Plotly HTML in browser)
- **Historical**: Solid line, circular markers
- **Forecast**: Dashed line, diamond markers
- **Separator**: Vertical dotted line at forecast start
- **Danger zone**: Red dotted line at 3.3V (if voltage data)
- **Hover**: Timestamp + exact value
- **Zoom/pan**: Enabled via Plotly controls
### Saved Script Output
Self-contained UV script:
```python
#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [...]
# ///

DEFAULT_CSV = 'full/path/to/your_file.csv'
DEFAULT_VALUES_COL = 'column_name'
DEFAULT_FORECAST_DAYS = 7
DEFAULT_SEASONAL_PERIOD = 24

# All functions included (load, clean, forecast, plot)
```

***

## How to Request Features

1. **Add to checklist** in "Core Features" section
2. **Describe use case** in "Use Cases" table
3. **Specify CLI changes** (new args, defaults)
4. **Specify input/output changes** (new formats, columns)
5. **Tag with priority**: `[P0]` (critical), `[P1]` (high), `[P2]` (nice-to-have)
**Example feature request format**:
```
- [ ] Export forecast to CSV [P1]
  - CLI: --export-path path/to/forecast.csv
  - Output: timestamp, value, is_forecast flag
  - Use case: Import forecast into other tools
```

***

## Implementation Notes for Developer

### Key Design Decisions
- **Date parsing**: `dateutil` fallback for maximum flexibility
- **Error handling**: Report and continue, never crash on bad data
- **Plotly**: Browser over tkinter (cross-platform, more features)
- **Script generation**: `exec()` style copying (simple but effective)
- **Default values**: Hardcoded in generated script (transparent, editable)