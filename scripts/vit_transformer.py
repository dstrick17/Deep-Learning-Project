import os
import pandas as pd
from PIL import Image
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from sklearn.model_selection import train_test_split
import torchvision.transforms as transforms
from tqdm.auto import tqdm
import wandb
from sklearn.metrics import roc_auc_score, f1_score
import numpy as np
from transformers import ViTForImageClassification, ViTFeatureExtractor
import time

# Configuration settings
CONFIG = {
    "model": "vit_transformer",
    "batch_size": 16,
    "learning_rate": 0.0001,
    "epochs": 20,
    "num_workers": 1,
    "device": "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu",
    "data_dir": "/projectnb/dl4ds/projects/dca_project/nih_data",
    "wandb_project": "X-Ray Classification",
    "patience": 5,
    "seed": 42,
    "image_size": 224,
}

# Define the model name and load feature extractor
model_name = "google/vit-base-patch16-224"
feature_extractor = ViTFeatureExtractor.from_pretrained(model_name)

# Define transform functions
def transform_train(img):
    return feature_extractor(images=img, return_tensors='pt')['pixel_values'][0]

def transform_test(img):
    return feature_extractor(images=img, return_tensors='pt')['pixel_values'][0]

# Load the CSV file with image metadata
data_path = CONFIG["data_dir"]
csv_file = os.path.join(data_path, "Data_Entry_2017.csv")
if not os.path.exists(csv_file):
    raise FileNotFoundError(f"CSV file not found at {csv_file}")
df = pd.read_csv(csv_file)

# Get list of all image folders from images_001 to images_012
image_folders = [os.path.join(data_path, f"images_{str(i).zfill(3)}", "images") for i in range(1, 13)]

# Create a dictionary mapping image filenames to their folder paths
image_to_folder = {}
for folder in image_folders:
    if os.path.exists(folder):
        for img_file in os.listdir(folder):
            if img_file.endswith('.png'):
                image_to_folder[img_file] = folder

# Filter the CSV to include only images that are present in the folders
df = df[df['Image Index'].isin(image_to_folder.keys())]
if df.empty:
    raise ValueError("No valid images found after filtering")

# Patient-level split
unique_patients = df['Patient ID'].unique()
train_val_patients, test_patients = train_test_split(unique_patients, test_size=0.2, random_state=CONFIG["seed"])
train_patients, val_patients = train_test_split(train_val_patients, test_size=0.25, random_state=CONFIG["seed"])
train_df = df[df['Patient ID'].isin(train_patients)]
val_df = df[df['Patient ID'].isin(val_patients)]
test_df = df[df['Patient ID'].isin(test_patients)]

# Verify splits
print(f"Train size: {len(train_df)}, Val size: {len(val_df)}, Test size: {len(test_df)}")
if len(train_df) == 0 or len(val_df) == 0 or len(test_df) == 0:
    raise ValueError("One or more data splits are empty")

# List of diseases we’re classifying
disease_list = [
    'Atelectasis', 'Cardiomegaly', 'Consolidation', 'Edema', 'Effusion',
    'Emphysema', 'Fibrosis', 'Hernia', 'Infiltration', 'Mass',
    'Nodule', 'Pleural_Thickening', 'Pneumonia', 'Pneumothorax'
]

# Function to convert label string to a vector
def get_label_vector(labels_str):
    labels = labels_str.split('|')
    if labels == ['No Finding']:
        return [0] * len(disease_list)
    else:
        return [1 if disease in labels else 0 for disease in disease_list]

# Custom Dataset class
class CheXNetDataset(Dataset):
    def __init__(self, dataframe, image_to_folder, transform=None):
        self.dataframe = dataframe
        self.image_to_folder = image_to_folder
        self.transform = transform

    def __len__(self):
        return len(self.dataframe)

    def __getitem__(self, idx):
        img_name = self.dataframe.iloc[idx]['Image Index']
        folder = self.image_to_folder[img_name]
        img_path = os.path.join(folder, img_name)
        image = Image.open(img_path).convert('RGB')
        if self.transform:
            image = self.transform(image)
        labels_str = self.dataframe.iloc[idx]['Finding Labels']
        label_vector = get_label_vector(labels_str)
        labels = torch.tensor(label_vector, dtype=torch.float)
        return image, labels

# Set up DataLoaders
train_dataset = CheXNetDataset(train_df, image_to_folder, transform=transform_train)
val_dataset = CheXNetDataset(val_df, image_to_folder, transform=transform_test)
test_dataset = CheXNetDataset(test_df, image_to_folder, transform=transform_test)

trainloader = DataLoader(train_dataset, batch_size=CONFIG["batch_size"], shuffle=True, num_workers=CONFIG["num_workers"])
valloader = DataLoader(val_dataset, batch_size=CONFIG["batch_size"], shuffle=False, num_workers=CONFIG["num_workers"])
testloader = DataLoader(test_dataset, batch_size=CONFIG["batch_size"], shuffle=False, num_workers=CONFIG["num_workers"])

# Load the pre-trained model
model = ViTForImageClassification.from_pretrained(
    model_name,
    num_labels=14,
    ignore_mismatched_sizes=True
)
model = model.to(CONFIG["device"])

# Define loss function and optimizer
criterion = nn.BCEWithLogitsLoss()
optimizer = optim.Adam(model.parameters(), lr=CONFIG["learning_rate"], weight_decay=1e-5)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=3, factor=0.1)

# Evaluation function
def evaluate(model, testloader, criterion, device, desc="[Test]"):
    model.eval()
    running_loss = 0.0
    all_labels = []
    all_preds = []
    with torch.no_grad():
        progress_bar = tqdm(testloader, desc=desc, leave=True)
        for inputs, labels in progress_bar:
            inputs, labels = inputs.to(device), labels.to(device)
            outputs = model(inputs).logits
            loss = criterion(outputs, labels)
            running_loss += loss.item()
            preds = torch.sigmoid(outputs)
            all_labels.append(labels.cpu())
            all_preds.append(preds.cpu())
    all_labels = torch.cat(all_labels).numpy()
    all_preds = torch.cat(all_preds).numpy()
    test_loss = running_loss / len(testloader)

    auc_scores = [roc_auc_score(all_labels[:, i], all_preds[:, i]) for i in range(14)]
    avg_auc = np.mean(auc_scores)
    for i, disease in enumerate(disease_list):
        print(f"{desc} {disease} AUC-ROC: {auc_scores[i]:.4f}")
    auc_dict = {disease_list[i]: auc_scores[i] for i in range(14)}

    preds_binary = (all_preds > 0.5).astype(int)
    f1_scores = [f1_score(all_labels[:, i], preds_binary[:, i]) for i in range(14)]
    avg_f1 = np.mean(f1_scores)
    for i, disease in enumerate(disease_list):
        print(f"{desc} {disease} F1 Score: {f1_scores[i]:.4f}")
    f1_dict = {disease_list[i]: f1_scores[i] for i in range(14)}
    print(f"{desc} Loss: {test_loss:.4f}, Avg AUC-ROC: {avg_auc:.4f}, Avg F1 Score: {avg_f1:.4f}")
    return test_loss, avg_auc, avg_f1, auc_dict, f1_dict

# Training function
def train(epoch, model, trainloader, optimizer, criterion, CONFIG):
    device = CONFIG["device"]
    model.train()
    running_loss = 0.0
    progress_bar = tqdm(trainloader, desc=f"Epoch {epoch+1}/{CONFIG['epochs']} [Train]", leave=True)
    # Ensure progress_bar is closed properly
    try:
        for i, (inputs, labels) in enumerate(progress_bar):
            inputs, labels = inputs.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(inputs).logits
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            running_loss += loss.item()
            progress_bar.set_postfix({"loss": running_loss / (i + 1)})
    finally:
        progress_bar.close()
    train_loss = running_loss / len(trainloader)
    return train_loss

def validate(model, valloader, criterion, device):
    val_loss, val_auc, val_f1, auc_dict, f1_dict = evaluate(model, valloader, criterion, device, desc="[Validate]")
    return val_loss, val_auc, val_f1, auc_dict, f1_dict

# Training loop with WandB and timestamped checkpoints
try:
    wandb.init(project=CONFIG["wandb_project"], config=CONFIG)
    wandb.watch(model)
except Exception as e:
    print(f"WandB initialization failed: {e}. Continuing without WandB.")
    wandb.init(mode="disabled")

run_id = wandb.run.id
checkpoint_dir = os.path.join("models", run_id)
os.makedirs(checkpoint_dir, exist_ok=True)

best_val_auc = 0.0
patience_counter = 0

for epoch in range(CONFIG["epochs"]):
    train_loss = train(epoch, model, trainloader, optimizer, criterion, CONFIG)
    val_loss, val_auc, val_f1, auc_dict, f1_dict = validate(model, valloader, criterion, CONFIG["device"])
    scheduler.step(val_loss)

    wandb.log({
        "epoch": epoch + 1,
        "train_loss": train_loss,
        "val_loss": val_loss,
        "val_auc": val_auc,
        "val_f1": val_f1,
        "f1_dict": f1_dict,
        "auc_dict": auc_dict,
    })

    if val_auc > best_val_auc:
        best_val_auc = val_auc
        patience_counter = 0
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        checkpoint_path = os.path.join(checkpoint_dir, f"best_model_{timestamp}.pth")
        torch.save(model.state_dict(), checkpoint_path)
        wandb.save(checkpoint_path)
    else:
        patience_counter += 1
        if patience_counter >= CONFIG["patience"]:
            print("Early stopping triggered.")
            break

# Evaluate the best model
checkpoint_files = [os.path.join(checkpoint_dir, f) for f in os.listdir(checkpoint_dir) if f.startswith('best_model_')]
if not checkpoint_files:
    raise FileNotFoundError("No checkpoint files found. Training may not have saved any models.")
best_checkpoint_path = sorted(checkpoint_files)[-1]
model.load_state_dict(torch.load(best_checkpoint_path))
test_loss, test_auc, test_f1, auc_dict, f1_dict = evaluate(model, testloader, criterion, CONFIG["device"])
wandb.log({
    "test_loss": test_loss,
    "test_auc": test_auc,
    "test_f1": test_f1,
    "test_auc_dict": auc_dict,
    "test_f1_dict": f1_dict
})

wandb.finish()