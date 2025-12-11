import argparse
import sys
from pathlib import Path
from typing import List

import numpy as np
from PIL import Image
from scipy import ndimage


def separate_components(
    input_path: Path,
    suffix: str = "-sep",
    padding: int = 5,
    threshold: int = 128,
    min_size: int = 10,
    verbose: bool = False,
) -> List[Path]:
    """
    Separate connected components in an image and save each as a separate file.
    
    Args:
        input_path: Path to input image
        suffix: Suffix to add before component number (e.g., "image-sep-001.png")
        padding: Pixels of padding around each component
        threshold: Threshold for binarization (pixels < threshold are considered foreground)
        min_size: Minimum number of pixels for a component to be saved
        verbose: Print detailed progress information
    
    Returns:
        List of paths to saved component images
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    if verbose:
        print(f"Processing: {input_path}")
    
    # Open and convert to grayscale
    try:
        img = Image.open(input_path).convert("L")
    except Exception as e:
        raise ValueError(f"Failed to open image {input_path}: {e}")
    
    img_array = np.array(img)
    
    # Invert so black graphics become white (for labeling)
    binary = img_array < threshold
    
    # Label connected components
    labeled, num_features = ndimage.label(binary)
    
    if verbose:
        print(f"Found {num_features} connected components")
    
    # Prepare output paths
    stem = input_path.stem
    ext = input_path.suffix
    parent = input_path.parent
    
    saved_paths = []
    saved_count = 0
    
    # Extract each component
    for i in range(1, num_features + 1):
        component_mask = labeled == i
        
        # Skip small components (likely noise)
        if np.sum(component_mask) < min_size:
            if verbose:
                print(f"Skipping component {i} (too small: {np.sum(component_mask)} pixels)")
            continue
        
        # Create image with white background and black foreground
        component = (~component_mask).astype(np.uint8) * 255
        
        # Find bounding box to crop (using the mask, not the inverted image)
        coords = np.where(component_mask)
        if len(coords[0]) == 0:
            continue
        
        y_min, y_max = coords[0].min(), coords[0].max()
        x_min, x_max = coords[1].min(), coords[1].max()
        
        # Add padding
        y_min = max(0, y_min - padding)
        x_min = max(0, x_min - padding)
        y_max = min(component.shape[0], y_max + padding)
        x_max = min(component.shape[1], x_max + padding)
        
        # Crop component
        cropped = component[y_min:y_max + 1, x_min:x_max + 1]
        
        # Convert back to PIL
        result = Image.fromarray(cropped, mode="L")
        
        # Save with suffix and component number
        saved_count += 1
        output_path = parent / f"{stem}{suffix}-{saved_count:03d}{ext}"
        result.save(output_path)
        saved_paths.append(output_path)
        
        if verbose:
            print(f"Saved component {i} -> {output_path}")
    
    print(f"Saved {saved_count} components from {input_path.name}")
    return saved_paths


def process_files(
    input_paths: List[Path],
    suffix: str,
    padding: int,
    threshold: int,
    min_size: int,
    verbose: bool,
) -> int:
    """Process multiple image files."""
    total_saved = 0
    errors = 0
    
    for input_path in input_paths:
        try:
            saved = separate_components(
                input_path,
                suffix=suffix,
                padding=padding,
                threshold=threshold,
                min_size=min_size,
                verbose=verbose,
            )
            total_saved += len(saved)
        except Exception as e:
            print(f"Error processing {input_path}: {e}", file=sys.stderr)
            errors += 1
    
    if errors > 0:
        print(f"\nCompleted with {errors} error(s)", file=sys.stderr)
    
    print(f"\nTotal components saved: {total_saved}")
    return 0 if errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Separate connected components in images into individual files"
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input image file(s) to process",
    )
    parser.add_argument(
        "-s", "--suffix",
        default="-sep",
        help="Suffix to add before component number (default: -sep)",
    )
    parser.add_argument(
        "-p", "--padding",
        type=int,
        default=5,
        help="Pixels of padding around each component (default: 5)",
    )
    parser.add_argument(
        "-t", "--threshold",
        type=int,
        default=128,
        help="Threshold for binarization, 0-255 (default: 128)",
    )
    parser.add_argument(
        "-m", "--min-size",
        type=int,
        default=10,
        help="Minimum pixels for a component to be saved (default: 10)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed progress information",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    
    # Validate inputs
    input_paths = []
    for path in args.inputs:
        if not path.exists():
            print(f"Error: File not found: {path}", file=sys.stderr)
            return 1
        if not path.is_file():
            print(f"Error: Not a file: {path}", file=sys.stderr)
            return 1
        input_paths.append(path)
    
    return process_files(
        input_paths,
        suffix=args.suffix,
        padding=args.padding,
        threshold=args.threshold,
        min_size=args.min_size,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
