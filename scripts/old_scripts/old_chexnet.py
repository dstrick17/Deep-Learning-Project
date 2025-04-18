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
import time
from focal_loss import FocalLoss

# Configuration settings
CONFIG = {
    "model": "chexnet",
    "batch_size": 16,
    "learning_rate": 0.001,  # Adjusted learning rate
    "epochs": 20,  # Adjusted epochs for snowy snow wandb run
    "patience": 5,  # Patience for learning rate decay
    "num_workers": 8,
    "device": "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu",
    "data_dir": "/projectnb/dl4ds/projects/dca_project/nih_data",
    "wandb_project": "X-Ray Classification",
    "seed": 42,
    "image_size": 224,  # Consistent image size
}

# Define image transformations (consistent with CheXNet)
transform_train = transforms.Compose([
    transforms.RandomResizedCrop(224),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),  # ImageNet normalization
])

transform_test = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])

# Load the CSV file with image metadata
data_path = CONFIG["data_dir"]
csv_file = os.path.join(data_path, "Data_Entry_2017.csv")
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

# Split the data into train+val and test (80% train+val, 20% test)
train_val_df, test_df = train_test_split(df, test_size=0.2, random_state=CONFIG["seed"])

# Further split train+val into train and val (75% train, 25% val of train+val)
train_df, val_df = train_test_split(train_val_df, test_size=0.25, random_state=CONFIG["seed"])  # 0.25 = 20/80

# ===== BEGIN OVERSAMPLING PNEUMONIA =====
from sklearn.utils import resample

# Separate pneumonia-positive and negative examples
pneumonia_pos = train_df[train_df['Finding Labels'].str.contains('Pneumonia')]
pneumonia_neg = train_df[~train_df['Finding Labels'].str.contains('Pneumonia')]

# Upsample positives to match negatives
pneumonia_pos_upsampled = resample(
    pneumonia_pos,
    replace=True,
    n_samples=len(pneumonia_neg),
    random_state=CONFIG["seed"]
)

# Combine
train_df = pd.concat([pneumonia_neg, pneumonia_pos_upsampled])
train_df = train_df.sample(frac=1, random_state=CONFIG["seed"]).reset_index(drop=True)
print(f"Oversampled train size: {len(train_df)} (Pneumonia: {pneumonia_pos_upsampled.shape[0]})")
# ===== END OVERSAMPLING =====


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

# Set up DataLoaders with our custom datasets
train_dataset = CheXNetDataset(train_df, image_to_folder, transform=transform_train)
val_dataset = CheXNetDataset(val_df, image_to_folder, transform=transform_test)
test_dataset = CheXNetDataset(test_df, image_to_folder, transform=transform_test)

trainloader = DataLoader(train_dataset, batch_size=CONFIG["batch_size"], shuffle=True, num_workers=CONFIG["num_workers"])
valloader = DataLoader(val_dataset, batch_size=CONFIG["batch_size"], shuffle=False, num_workers=CONFIG["num_workers"])
testloader = DataLoader(test_dataset, batch_size=CONFIG["batch_size"], shuffle=False, num_workers=CONFIG["num_workers"])

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
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            running_loss += loss.item()
            preds = torch.sigmoid(outputs)
            all_labels.append(labels.cpu())
            all_preds.append(preds.cpu())

    all_labels = torch.cat(all_labels).numpy()
    all_preds = torch.cat(all_preds).numpy()
    test_loss = running_loss / len(testloader)

    # Compute AUC for each class
    auc_scores = [roc_auc_score(all_labels[:, i], all_preds[:, i]) for i in range(14)]
    avg_auc = np.mean(auc_scores)
    for i, disease in enumerate(disease_list):
        print(f"{desc} {disease} AUC-ROC: {auc_scores[i]:.4f}")
    auc_dict = {disease_list[i]: auc_scores[i] for i in range(14)}

    # Compute macro F1 score across all 14 diseases with default 0.5 threshold
    preds_binary = (all_preds > 0.5).astype(int)
    f1 = f1_score(all_labels, preds_binary, average='macro')
    print(f"{desc} Loss: {test_loss:.4f}, Avg AUC-ROC: {avg_auc:.4f}, F1 Score: {f1:.4f}")

    # === Optimize Pneumonia threshold ===
    from sklearn.metrics import precision_recall_curve

    def optimize_threshold(y_true, y_probs):
        precision, recall, thresholds = precision_recall_curve(y_true, y_probs)
        f1_scores = 2 * (precision * recall) / (precision + recall + 1e-8)
        best_threshold = thresholds[np.argmax(f1_scores)]
        return best_threshold

    pneumonia_index = disease_list.index('Pneumonia')
    pneumonia_labels = all_labels[:, pneumonia_index]
    pneumonia_preds = all_preds[:, pneumonia_index]
    best_threshold = optimize_threshold(pneumonia_labels, pneumonia_preds)
    pneumonia_preds_binary = (pneumonia_preds > best_threshold).astype(int)
    pneumonia_f1 = f1_score(pneumonia_labels, pneumonia_preds_binary)

    print(f"{desc} Pneumonia Optimal Threshold: {best_threshold:.2f}")
    print(f"{desc} Pneumonia F1 Score: {pneumonia_f1:.4f}")

    return test_loss, avg_auc, f1, pneumonia_f1, auc_dict

# Load and modify the model
model = torch.hub.load('pytorch/vision:v0.10.0', 'densenet121', pretrained=True)
model.classifier = nn.Linear(model.classifier.in_features, 14)
model = model.to(CONFIG["device"])

# Define loss function and optimizer
pneumonia_index = disease_list.index('Pneumonia')
# weights = torch.ones(14).to(CONFIG["device"])
# weights[pneumonia_index] = 5.0  # Increase weight for pneumonia
# criterion = nn.BCEWithLogitsLoss(pos_weight = weights)
criterion = FocalLoss(alpha=1, gamma=2)  # You can tune alpha/gamma later

optimizer = optim.Adam(model.parameters(), lr=CONFIG["learning_rate"], weight_decay=1e-5)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=CONFIG["patience"], factor=0.1)
# Training function
def train(epoch, model, trainloader, optimizer, criterion, CONFIG):
    device = CONFIG["device"]
    model.train()
    running_loss = 0.0
    progress_bar = tqdm(trainloader, desc=f"Epoch {epoch+1}/{CONFIG['epochs']} [Train]", leave=True)
    for i, (inputs, labels) in enumerate(progress_bar):
        inputs, labels = inputs.to(device), labels.to(device)
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        running_loss += loss.item()
        progress_bar.set_postfix({"loss": running_loss / (i + 1)})
    train_loss = running_loss / len(trainloader)
    return train_loss
    
# Validation function
def validate(model, valloader, criterion, device):
    val_loss, val_auc, val_f1, val_pneumonia_f1, auc_dict = evaluate(model, valloader, criterion, device, desc="[Validate]")
    return val_loss, val_auc, val_f1, val_pneumonia_f1, auc_dict

# Training loop with WandB and timestamped checkpoints
wandb.init(project=CONFIG["wandb_project"], config=CONFIG)
wandb.watch(model)
run_id = wandb.run.id
checkpoint_dir = os.path.join("models", run_id)
os.makedirs(checkpoint_dir, exist_ok=True)

best_val_auc = 0.0
best_val_pneumonia_f1 = 0.0
patience_counter = 0

for epoch in range(CONFIG["epochs"]):
    train_loss = train(epoch, model, trainloader, optimizer, criterion, CONFIG)
    val_loss, val_auc, val_f1, val_pneumonia_f1, auc_dict = validate(model, valloader, criterion, CONFIG["device"])
    scheduler.step(val_loss)

    wandb.log({
        "epoch": epoch + 1,
        "train_loss": train_loss,
        "val_loss": val_loss,
        "val_auc": val_auc,
        "val_f1": val_f1,
        "val_pneumonia_f1": val_pneumonia_f1,
        "auc_dict": auc_dict,
    })

    if val_pneumonia_f1 > best_val_pneumonia_f1:
        best_val_pneumonia_f1 = val_pneumonia_f1
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
best_checkpoint_path = sorted([os.path.join(checkpoint_dir, f) for f in os.listdir(checkpoint_dir) if f.startswith('best_model_')])[-1]
model.load_state_dict(torch.load(best_checkpoint_path))
test_loss, test_auc, test_f1, test_pneumonia_f1, auc_dict = evaluate(model, testloader, criterion, CONFIG["device"])
wandb.log({"test_loss": test_loss, "test_auc": test_auc, "test_f1": test_f1, "test_pneumonia_f1": test_pneumonia_f1, "test_auc_dict": auc_dict})
wandb.finish()