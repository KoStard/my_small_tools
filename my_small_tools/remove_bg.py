from PIL import Image
import argparse
from pathlib import Path
from typing import List


def remove_gray_background(
    input_path: Path,
    output_path: Path,
    tolerance: int = 30,
    brightness_threshold: int = 220
) -> None:
    """
    Remove gray background from an image by replacing gray pixels with white.
    
    Args:
        input_path: Path to input image
        output_path: Path to save output image
        tolerance: Maximum difference between R, G, B values to consider a pixel gray
        brightness_threshold: Pixels brighter than this are not replaced
    """
    # Open image
    img = Image.open(input_path).convert("RGB")
    pixels = img.load()

    # Get image dimensions
    width, height = img.size

    # Replace gray with white
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]

            # Check if pixel is "gray" (R ≈ G ≈ B)
            # and not too dark (not black) and not too light (not white)
            if abs(r - g) < tolerance and abs(g - b) < tolerance and abs(r - b) < tolerance:
                if 100 < r:
                    pixels[x, y] = (255, 255, 255)  # Replace with white

    img.save(output_path)
    print(f"Processed: {input_path} -> {output_path}")


def process_images(
    input_paths: List[Path],
    suffix: str = "-nobg",
    tolerance: int = 30,
    brightness_threshold: int = 220
) -> None:
    """
    Process multiple images to remove gray backgrounds.
    
    Args:
        input_paths: List of input image paths
        suffix: Suffix to add to output filenames (before extension)
        tolerance: Maximum difference between R, G, B values to consider a pixel gray
        brightness_threshold: Pixels brighter than this are not replaced
    """
    for input_path in input_paths:
        if not input_path.exists():
            print(f"Warning: File not found: {input_path}")
            continue
        
        if not input_path.is_file():
            print(f"Warning: Not a file: {input_path}")
            continue
        
        # Create output path in same directory with suffix
        output_path = input_path.parent / f"{input_path.stem}{suffix}{input_path.suffix}"
        
        try:
            remove_gray_background(input_path, output_path, tolerance, brightness_threshold)
        except Exception as e:
            print(f"Error processing {input_path}: {e}")


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Remove gray backgrounds from images by replacing gray pixels with white."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input image file(s) to process"
    )
    parser.add_argument(
        "-s", "--suffix",
        default="-nobg",
        help="Suffix to add to output filenames (default: -nobg)"
    )
    parser.add_argument(
        "-t", "--tolerance",
        type=int,
        default=30,
        help="Maximum difference between R, G, B values to consider a pixel gray (default: 30)"
    )
    parser.add_argument(
        "-b", "--brightness-threshold",
        type=int,
        default=220,
        help="Pixels brighter than this value are not replaced (default: 220)"
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    
    process_images(
        input_paths=args.inputs,
        suffix=args.suffix,
        tolerance=args.tolerance,
        brightness_threshold=args.brightness_threshold
    )


if __name__ == "__main__":
    main()
