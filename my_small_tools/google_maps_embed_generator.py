#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Generate Google Maps embed iframe from a Google Maps URL.

Extracts coordinates, zoom level, map type, heading, and tilt from various
Google Maps URL formats and generates an embeddable iframe.
"""

import sys
import re
import math
import urllib.parse

def extract_params(url):
    """Extract map parameters from Google Maps URL.
    
    Args:
        url: Google Maps URL
        
    Returns:
        Tuple of (lat, lon, zoom, layer, heading, tilt)
    """
    # 1. Extract Lat/Lon
    coords_match = re.search(r'@([-.\d]+),([-.\d]+)', url)
    lat, lon = coords_match.groups() if coords_match else (None, None)

    # 2. Extract Zoom or Altitude
    zoom = "15"  # Default zoom

    # Try finding standard zoom (e.g., 15z)
    z_match = re.search(r'([\d.]+)z', url)
    if z_match:
        zoom = z_match.group(1)
    else:
        # Try finding altitude (e.g., 189a)
        a_match = re.search(r'([\d.]+)a', url)
        if a_match:
            alt = float(a_match.group(1))
            # Convert altitude to zoom level
            # Empirical formula: at zoom 19, altitude is roughly 200m
            # Each zoom level doubles/halves the altitude
            try:
                converted_z = round(19 - math.log2(alt / 200))
                zoom = str(max(1, min(21, converted_z)))
            except (ValueError, ZeroDivisionError):
                zoom = "18"

    # 3. Detect Layers (Satellite/Hybrid)
    layer = ""
    if "!1e3" in url or "layer=s" in url or "/data=.*!3m1!1e3" in url:
        layer = "&t=k"  # k = satellite
    elif "!1e2" in url or "layer=h" in url:
        layer = "&t=h"  # h = hybrid (satellite + labels)

    # 4. Extract Heading (compass direction)
    # Heading is usually encoded as e.g., 90h (90 degrees)
    heading = None
    h_match = re.search(r'([\d.]+)h', url)
    if h_match:
        heading = h_match.group(1)

    # 5. Extract Tilt (pitch angle for 3D view)
    # Tilt is usually encoded as e.g., 45t (45 degrees)
    tilt = None
    t_match = re.search(r'([\d.]+)t', url)
    if t_match:
        tilt = t_match.group(1)

    return lat, lon, zoom, layer, heading, tilt

def build_embed_url(lat, lon, zoom, layer, heading, tilt):
    """Build the embed URL with all parameters."""
    base_url = "https://maps.google.com/maps"
    params = {
        "q": f"loc:{lat},{lon}",
        "iwloc": "J",
        "output": "embed",
        "z": zoom,
    }
    
    url = f"{base_url}?{urllib.parse.urlencode(params)}{layer}"
    
    # Note: Heading and tilt work best with 3D/Earth view
    # These may not work reliably with the embed API
    if heading:
        url += f"&heading={heading}"
    if tilt:
        url += f"&tilt={tilt}"
    
    return url

def main():
    """Main entry point for the script."""
    if len(sys.argv) < 2:
        print("Usage: google_maps_embed_generator.py <google_maps_url> [width] [height]", file=sys.stderr)
        print("\nExamples:", file=sys.stderr)
        print("  google_maps_embed_generator.py 'https://maps.google.com/@35.6762,139.6503,15z'", file=sys.stderr)
        print("  google_maps_embed_generator.py 'https://maps.google.com/@35.6762,139.6503,189a,35y,45t' 600 400", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    width = sys.argv[2] if len(sys.argv) > 2 else "600"
    height = sys.argv[3] if len(sys.argv) > 3 else "450"

    lat, lon, zoom, layer, heading, tilt = extract_params(url)

    if lat and lon:
        embed_url = build_embed_url(lat, lon, zoom, layer, heading, tilt)
        iframe = f'<iframe src="{embed_url}" width="{width}" height="{height}" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>'
        print(iframe)
        
        # Also print useful info to stderr
        print(f"\n# Extracted parameters:", file=sys.stderr)
        print(f"# Coordinates: {lat}, {lon}", file=sys.stderr)
        print(f"# Zoom: {zoom}", file=sys.stderr)
        if layer:
            print(f"# Layer: {'Satellite' if 'k' in layer else 'Hybrid'}", file=sys.stderr)
        if heading:
            print(f"# Heading: {heading}°", file=sys.stderr)
        if tilt:
            print(f"# Tilt: {tilt}°", file=sys.stderr)
    else:
        print("Error: Could not find coordinates in the provided URL.", file=sys.stderr)
        print("Make sure the URL contains coordinates in the format: @lat,lon", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
