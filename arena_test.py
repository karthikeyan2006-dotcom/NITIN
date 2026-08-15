import torch
import timm
from torchvision import transforms
from PIL import Image, ImageDraw, ImageFont
import os
import shutil
from pathlib import Path
from ultralytics import YOLO
from datetime import datetime
import io
import base64
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import numpy as np

# ===== CONFIGURATION =====
CONFIG = {
    'yolo_model_path': r"D:/nitin_project/models/best.pt",
    'classification_model_path': r"D:/nitin_project/models/transfer.pth",
    'input_folder': r"D:/sem4_project/input2",
    'output_object_folder': r"D:/sem4_project/test/ship",
    'output_no_object_folder': r"D:/sem4_project/test/no_ship",
    'info_folder': r"D:/sem4_project/test/info",
    'confidence_threshold': 0.25,

    # MongoDB Configuration
    'mongodb_uri': 'mongodb://localhost:27017/',
    'mongodb_database': 'ship_detection_db',
    'store_original_image': True,
    'store_annotated_image': True,
}

# ===== CLASS NAMES (FGSC-23 Dataset) =====
class_names = [
    'non-ship',                                    # 0
    'air carrier',                                 # 1
    'destroyer',                                   # 2
    'landing craft',                               # 3
    'frigate',                                     # 4
    'amphibious transport dock',                   # 5
    'cruiser',                                     # 6
    'Tarawa-class amphibious assault ship',        # 7
    'amphibious assault ship',                     # 8
    'command ship',                                # 9
    'submarine',                                   # 10
    'medical ship',                                # 11
    'combat boat',                                 # 12
    'auxiliary ship',                              # 13
    'container ship',                              # 14
    'car carrier',                                 # 15
    'hovercraft',                                  # 16
    'bulk carrier',                                # 17
    'oil tanker',                                  # 18
    'fishing boat',                                # 19
    'passenger ship',                              # 20
    'liquefied gas ship',                          # 21
    'barge'                                        # 22
]

# ===== MONGODB CONNECTION =====
def connect_to_mongodb():
    """Connect to MongoDB and return database instance"""
    try:
        print("Connecting to MongoDB...")
        client = MongoClient(CONFIG['mongodb_uri'], serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        db = client[CONFIG['mongodb_database']]
        print(f"✓ Connected to MongoDB database: '{CONFIG['mongodb_database']}'")
        return db, client
    except ConnectionFailure as e:
        print(f"✗ Failed to connect to MongoDB: {e}")
        print("\nMake sure MongoDB is running:")
        print("  - Windows: Check MongoDB service in Services")
        print("  - Linux/Mac: Run 'sudo systemctl start mongod'")
        return None, None
    except Exception as e:
        print(f"✗ MongoDB connection error: {e}")
        return None, None

# ===== IMAGE TO BASE64 =====
def image_to_base64(image_path):
    """Convert image file to base64 string"""
    with open(image_path, 'rb') as img_file:
        return base64.b64encode(img_file.read()).decode('utf-8')

def pil_image_to_base64(pil_image):
    """Convert PIL Image to base64 string"""
    buffer = io.BytesIO()
    pil_image.save(buffer, format='JPEG', quality=95)
    return base64.b64encode(buffer.getvalue()).decode('utf-8')

# ===== GET IMAGE DATETIME =====
def get_image_datetime(image_path):
    """
    Extract datetime from image EXIF data or use file creation time
    Returns datetime object
    """
    try:
        from PIL.ExifTags import TAGS
        image = Image.open(image_path)
        exif_data = image._getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTime' or tag == 'DateTimeOriginal':
                    try:
                        return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
                    except:
                        pass
    except:
        pass
    timestamp = os.path.getmtime(image_path)
    return datetime.fromtimestamp(timestamp)

# ===== SETUP FOLDERS =====
def setup_folders():
    """Create necessary folders if they don't exist"""
    folders = [
        CONFIG['input_folder'],
        CONFIG['output_object_folder'],
        CONFIG['output_no_object_folder'],
        CONFIG['info_folder']
    ]
    for folder in folders:
        Path(folder).mkdir(parents=True, exist_ok=True)
    print("✓ Folders setup complete")

# ===== INITIALIZE MODELS =====
def initialize_models():
    """Initialize YOLO and Classification models"""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    print("Loading YOLO model...")
    yolo_model = YOLO(CONFIG['yolo_model_path'])
    print("✓ YOLO model loaded")

    print("Loading Classification model...")
    classification_model = timm.create_model(
        'convnext_large',
        pretrained=False,
        num_classes=len(class_names)
    )
    classification_model.load_state_dict(
        torch.load(CONFIG['classification_model_path'], map_location=device)
    )
    classification_model.to(device)
    classification_model.eval()
    print("✓ Classification model loaded")

    return yolo_model, classification_model, device

# ===== IMAGE TRANSFORM FOR CLASSIFICATION =====
transform = transforms.Compose([
    transforms.Resize((384, 384)),
    transforms.ToTensor(),
    transforms.Normalize(
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225]
    )
])

# ===== YOLO DETECTION =====
def detect_objects(yolo_model, image_path):
    """
    Run YOLO detection on image
    Returns: (has_objects, results)
    """
    results = yolo_model(image_path, conf=CONFIG['confidence_threshold'])
    has_objects = len(results[0].boxes) > 0
    return has_objects, results[0]

# ===== CLASSIFICATION =====
def classify_image(model, image_path, device):
    """
    Classify image using the classification model
    Returns: (predicted_class, confidence_score)
    """
    image = Image.open(image_path).convert("RGB")
    image_tensor = transform(image).unsqueeze(0).to(device)

    with torch.no_grad():
        outputs = model(image_tensor)
        probabilities = torch.softmax(outputs, dim=1)
        confidence, predicted = torch.max(probabilities, 1)

    predicted_class = class_names[predicted.item()]
    confidence_score = confidence.item()

    return predicted_class, confidence_score

# ===== ANNOTATE IMAGE =====
def annotate_image(image_path, yolo_results, classification_result, confidence):
    """
    Annotate image with YOLO bounding boxes and classification result
    Returns: annotated PIL Image
    """
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype("arial.ttf", 20)
        font_small = ImageFont.truetype("arial.ttf", 16)
    except:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()

    for box in yolo_results.boxes:
        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
        draw.rectangle([x1, y1, x2, y2], outline="lime", width=3)
        det_conf = box.conf[0].item()
        draw.text((x1, y1 - 25), f"Det: {det_conf*100:.1f}%",
                  fill="lime", font=font_small)

    class_text = f"Predicted Class: {classification_result}"
    conf_text = f"Confidence: {confidence*100:.2f}%"

    text_bbox = draw.textbbox((10, 10), class_text, font=font)
    draw.rectangle([5, 5, text_bbox[2] + 10, 70], fill="black", outline="lime", width=2)

    draw.text((10, 10), class_text, fill="lime", font=font)
    draw.text((10, 40), conf_text, fill="lime", font=font)

    return image

# ===== SAVE SHIP DATA TO MONGODB (ONLY FOR SHIPS) =====
def save_ship_to_mongodb(db, image_name, image_path, yolo_results, classification_result,
                         confidence, image_datetime, annotated_image=None):
    """
    Save detection and classification data to MongoDB - ONLY for ship detections.
    No-ship / no-detection data is NOT stored in MongoDB.
    Collection name is based on date (YYYY-MM-DD)
    """
    if db is None:
        print("  ⚠ Skipping MongoDB save (not connected)")
        return False

    try:
        collection_name = image_datetime.strftime('%Y-%m-%d')
        collection = db[collection_name]

        # Prepare bounding boxes data
        bounding_boxes = []
        for idx, box in enumerate(yolo_results.boxes, 1):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            det_conf = box.conf[0].item()
            bounding_boxes.append({
                'object_number': idx,
                'coordinates': {
                    'x1': float(x1),
                    'y1': float(y1),
                    'x2': float(x2),
                    'y2': float(y2)
                },
                'detection_confidence': float(det_conf)
            })

        # Prepare document
        document = {
            'image_name': image_name,
            'date_time': image_datetime,
            'processed_at': datetime.now(),
            'ship_class': classification_result,
            'classification_confidence': float(confidence),
            'number_of_objects': len(yolo_results.boxes),
            'bounding_boxes': bounding_boxes,
        }

        # Add original image if configured
        if CONFIG['store_original_image']:
            document['original_image'] = image_to_base64(image_path)
            document['original_image_format'] = 'base64_jpeg'

        # Add annotated image if configured
        if CONFIG['store_annotated_image'] and annotated_image:
            document['annotated_image'] = pil_image_to_base64(annotated_image)
            document['annotated_image_format'] = 'base64_jpeg'

        # Insert document
        result = collection.insert_one(document)
        print(f"  ✓ Ship data saved to MongoDB collection '{collection_name}' (ID: {result.inserted_id})")
        return True

    except Exception as e:
        print(f"  ✗ Error saving to MongoDB: {e}")
        return False

# ===== SAVE INFO TO FILE =====
def save_info(image_name, yolo_results, classification_result, confidence, image_datetime):
    """Save detection and classification info to text file"""
    info_file = os.path.join(CONFIG['info_folder'], f"{Path(image_name).stem}_info.txt")

    with open(info_file, 'w') as f:
        f.write(f"Image: {image_name}\n")
        f.write(f"Image Date/Time: {image_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Processed At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"=" * 50 + "\n\n")

        f.write("YOLO DETECTION RESULTS:\n")
        f.write(f"Number of objects detected: {len(yolo_results.boxes)}\n\n")

        for idx, box in enumerate(yolo_results.boxes, 1):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            det_conf = box.conf[0].item()
            f.write(f"Object {idx}:\n")
            f.write(f"  Bounding Box: ({x1:.1f}, {y1:.1f}, {x2:.1f}, {y2:.1f})\n")
            f.write(f"  Detection Confidence: {det_conf*100:.2f}%\n\n")

        f.write("=" * 50 + "\n\n")
        f.write("CLASSIFICATION RESULTS:\n")
        f.write(f"Predicted Class: {classification_result}\n")
        f.write(f"Classification Confidence: {confidence*100:.2f}%\n")

    print(f"  ✓ Info saved to: {info_file}")

# ===== PROCESS SINGLE IMAGE =====
def process_image(image_path, yolo_model, classification_model, device, db):
    """Process a single image through the pipeline"""
    image_name = os.path.basename(image_path)
    print(f"\nProcessing: {image_name}")

    # Get image datetime
    image_datetime = get_image_datetime(image_path)
    print(f"  Image date/time: {image_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: YOLO Detection
    print("  Running YOLO detection...")
    has_objects, yolo_results = detect_objects(yolo_model, image_path)

    if not has_objects:
        # No objects detected - move to no_object folder (NO MongoDB save)
        print("  ✗ No objects detected")
        dest_path = os.path.join(CONFIG['output_no_object_folder'], image_name)
        shutil.move(image_path, dest_path)
        print(f"  → Moved to: {dest_path}")
        print("  ⏭ Skipping MongoDB save (no ship detected)")
        return

    print(f"  ✓ {len(yolo_results.boxes)} object(s) detected")

    # Step 2: Classification
    print("  Running classification...")
    predicted_class, confidence_score = classify_image(classification_model, image_path, device)
    print(f"  ✓ Classified as: {predicted_class} ({confidence_score*100:.2f}%)")

    # Step 3: Annotate image
    print("  Annotating image...")
    annotated_image = annotate_image(image_path, yolo_results, predicted_class, confidence_score)

    # Step 4: Save to MongoDB (ONLY ship data)
    print("  Saving ship data to MongoDB...")
    save_ship_to_mongodb(db, image_name, image_path, yolo_results, predicted_class,
                         confidence_score, image_datetime, annotated_image)

    # Step 5: Save annotated image to object folder
    dest_path = os.path.join(CONFIG['output_object_folder'], image_name)
    annotated_image.save(dest_path)
    print(f"  ✓ Annotated image saved to: {dest_path}")

    # Step 6: Save info to text file
    save_info(image_name, yolo_results, predicted_class, confidence_score, image_datetime)

    # Step 7: Remove original image from input folder
    os.remove(image_path)
    print(f"  ✓ Original image removed from input folder")

# ===== MAIN PIPELINE =====
def main():
    """Main pipeline execution"""
    print("\n" + "=" * 60)
    print("DETECTION & CLASSIFICATION PIPELINE WITH MONGODB")
    print("(Only ship data stored in MongoDB)")
    print("=" * 60 + "\n")

    # Setup
    setup_folders()

    # Connect to MongoDB
    db, mongo_client = connect_to_mongodb()
    if db is None:
        print("\n⚠ WARNING: Pipeline will continue without MongoDB storage")
        response = input("Continue anyway? (y/n): ")
        if response.lower() != 'y':
            print("Exiting...")
            return

    # Initialize models
    yolo_model, classification_model, device = initialize_models()

    # Get all images from input folder
    input_folder = Path(CONFIG['input_folder'])
    image_extensions = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff']
    image_files = [
        f for f in input_folder.iterdir()
        if f.suffix.lower() in image_extensions
    ]

    if not image_files:
        print("\n⚠ No images found in input folder!")
        if mongo_client is not None:
            mongo_client.close()
        return

    print(f"\nFound {len(image_files)} image(s) to process\n")
    print("=" * 60)

    # Process each image
    for idx, image_file in enumerate(image_files, 1):
        print(f"\n[{idx}/{len(image_files)}]", end=" ")
        try:
            process_image(str(image_file), yolo_model, classification_model, device, db)
        except Exception as e:
            print(f"  ✗ Error processing {image_file.name}: {str(e)}")

    # Summary
    print("\n" + "=" * 60)
    print("PROCESSING COMPLETE!")
    print("=" * 60)
    print(f"\nResults:")

    object_count = len(list(Path(CONFIG['output_object_folder']).iterdir()))
    no_object_count = len(list(Path(CONFIG['output_no_object_folder']).iterdir()))
    print(f"  • Images with objects (ships) detected: {object_count}")
    print(f"  • Images with no objects (no ships): {no_object_count}")
    print(f"  • Info files created: {len(list(Path(CONFIG['info_folder']).iterdir()))}")

    if db is not None:
        print(f"\n  • MongoDB Database: '{CONFIG['mongodb_database']}'")
        print(f"  • Collections created (ship data only): {db.list_collection_names()}")
        print(f"  • Only ship detections stored in MongoDB ({object_count} records)")
    print()

    # Close MongoDB connection
    if mongo_client is not None:
        mongo_client.close()
        print("✓ MongoDB connection closed")

if __name__ == "__main__":
    main()