import env_bootstrap  # noqa: F401  (must be imported before any qgis/pcraster import)
import customtkinter as ctk
from tkinter import filedialog, messagebox, Text, END, font
import threading
import os
import sys
import subprocess
from CatchmentRunoff import run_geospatial_analysis
from qgis.core import QgsApplication
from qgis.analysis import QgsNativeAlgorithms

# Define the TextRedirector class
class TextRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, message):
        self.widget.insert(END, message)
        self.widget.see(END)

    def flush(self):
        pass  # Not needed for this implementation

# Function to get only the last part of a path
def get_last_part(path):
    return os.path.basename(path)

# Initialize the main application window
root = ctk.CTk()
root.title("Geospatial Analysis Tool")
root.geometry("730x640")  # Increased height
root.resizable(True, True)

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# Create dictionary to store full paths
full_paths = {}

# Function to select directory
def select_directory(entry, title, key):
    directory = filedialog.askdirectory(title=title)
    if directory:
        full_paths[key] = directory
        entry.delete(0, ctk.END)
        entry.insert(0, get_last_part(directory))
        auto_fill_paths(directory)

# Function to select file
def select_file(entry, title, key):
    file_path = filedialog.askopenfilename(title=title, filetypes=[("All files", "*.*")])
    if file_path:
        full_paths[key] = file_path
        entry.delete(0, ctk.END)
        entry.insert(0, get_last_part(file_path))

# Automatically fill paths based on the selected main folder
def auto_fill_paths(input_directory):
    input_folder = os.path.join(input_directory, "Input")
    shp_extract_path_entry.delete(0, ctk.END)
    shp_id15_path_entry.delete(0, ctk.END)
    input_raster_path_entry.delete(0, ctk.END)
    dtm_raster_path_entry.delete(0, ctk.END)
    
    for root, _, files in os.walk(input_folder):
        for file in files:
            file_path = os.path.join(root, file)
            file_path = os.path.normpath(file_path)
            if "id15" in file.lower() and file.lower().endswith("d15.gpkg"):
                full_paths['shp_id15'] = file_path
                shp_id15_path_entry.insert(0, get_last_part(file_path))
            elif "input_polygon" in file.lower() and file.lower().endswith("ygon.gpkg"):
                full_paths['shp_extract'] = file_path
                shp_extract_path_entry.insert(0, get_last_part(file_path))
            elif "ldd" in file.lower() and file.lower().endswith("dd25832.map"):
                full_paths['input_raster'] = file_path
                input_raster_path_entry.insert(0, get_last_part(file_path))
            elif "dtm" in file.lower() and file.lower().endswith((".tif", ".dem")):
                full_paths['dtm_raster'] = file_path
                dtm_raster_path_entry.insert(0, get_last_part(file_path))

# Create labeled entry and button pairs for file/directory selections
def create_label_entry_button_pair(root, text, row, col, key, open_file_dialog=False, open_directory_dialog=False, placeholder="", title=""):
    label = ctk.CTkLabel(root, text=text, anchor='w', width=150)
    label.grid(row=row, column=col, padx=(10, 5), pady=5, sticky='e')
    entry = ctk.CTkEntry(root, width=150, placeholder_text=placeholder)
    entry.grid(row=row, column=col+1, padx=(5, 10), pady=5, sticky='w')
    if open_file_dialog:
        entry.bind("<Button-1>", lambda e: select_file(entry, title, key))
    elif open_directory_dialog:
        entry.bind("<Button-1>", lambda e: select_directory(entry, title, key))
    return entry

# Create input fields for paths and parameters with titles
input_directory_entry = create_label_entry_button_pair(root, "Input Directory:", 0, 0, 'input_directory', open_directory_dialog=True, placeholder="Choose input folder", title="Select Input Directory")
shp_extract_path_entry = create_label_entry_button_pair(root, "Input Polygon:", 1, 0, 'shp_extract', open_file_dialog=True, placeholder="Choose input polygon", title="Select Input Polygon File")
shp_id15_path_entry = create_label_entry_button_pair(root, "ID15 File:", 2, 0, 'shp_id15', open_file_dialog=True, placeholder="Choose ID15 layer", title="Select ID15 File")
input_raster_path_entry = create_label_entry_button_pair(root, "LDD File:", 3, 0, 'input_raster', open_file_dialog=True, placeholder="Choose LDD File", title="Select LDD File")
dtm_raster_path_entry = create_label_entry_button_pair(root, "DTM File:", 4, 0, 'dtm_raster', open_file_dialog=True, placeholder="Choose DTM File", title="Select DTM File")
crs_entry = create_label_entry_button_pair(root, "CRS (EPSG):", 5, 0, 'crs', placeholder="25832")
crs_entry.insert(0, "25832")

# Set default values for some entries
num_top_stream_order_values_1_entry = create_label_entry_button_pair(root, "Strahler Order ID15:", 0, 2, 'num_top_stream_order_values_1', placeholder="Define strahler order for ID15")
num_top_stream_order_values_1_entry.insert(0, "4")
num_top_stream_order_values_2_entry = create_label_entry_button_pair(root, "Strahler Order AOI:", 1, 2, 'num_top_stream_order_values_2', placeholder="Define strahler order for area of interest")
num_top_stream_order_values_2_entry.insert(0, "4")
selected_rainfall_scenario_entry = create_label_entry_button_pair(root, "Rainfall Scenario:", 2, 2, 'selected_rainfall_scenario', placeholder="Define Rainfall Scenario")
selected_rainfall_scenario_entry.insert(0, "60")
qgis_project_name_entry = create_label_entry_button_pair(root, "QGIS Project Name:", 3, 2, 'qgis_project_name', placeholder="Set QGIS Project Name")
qgis_project_name_entry.insert(0, "Geospatial_Project")

# Create a checkbox to allow the user to choose whether to open the results after completion
open_results_var = ctk.IntVar(value=1)  # Set default to checked
open_results_checkbox = ctk.CTkCheckBox(root, text="Open results after completion", variable=open_results_var)
open_results_checkbox.grid(row=4, column=2, columnspan=4, padx=(28, 0), pady=0, sticky='w')

# Logging Text Box
log_text = Text(root, wrap='word', height=20, width=90, bg="black", fg="white", font=("Consolas", 22, "bold"))
log_text.grid(row=7, column=0, columnspan=4, padx=10, pady=10, sticky='ew')

# Redirect print statements to the log text box
sys.stdout = TextRedirector(log_text)

# Initialize QGIS application in the main thread
QgsApplication.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
qgs = QgsApplication([], False)
qgs.initQgis()
QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

import subprocess
import os

# Function to run the analysis
def run_analysis():
    input_directory = full_paths.get('input_directory', '')
    shp_extract_path = full_paths.get('shp_extract', '')
    shp_id15_path = full_paths.get('shp_id15', '')
    input_raster_path = full_paths.get('input_raster', '')
    dtm_raster_path = full_paths.get('dtm_raster', '')
    
    # Path length check
    paths = [input_directory, shp_extract_path, shp_id15_path, input_raster_path, dtm_raster_path]
    max_path_length = 260  # Common maximum path length in Windows

    for path in paths:
        if len(path) > max_path_length:
            messagebox.showerror("Error", f"Path too long: {path}")
            return

    num_top_stream_order_values_1 = int(num_top_stream_order_values_1_entry.get())
    num_top_stream_order_values_2 = int(num_top_stream_order_values_2_entry.get())
    selected_rainfall_scenario = int(selected_rainfall_scenario_entry.get())
    crs = crs_entry.get()
    qgis_project_name = qgis_project_name_entry.get()

    if not crs.upper().startswith("EPSG:"):
        crs = f"EPSG:{crs}"

    try:
        print("Running analysis...")
        run_geospatial_analysis(
            input_directory, 
            num_top_stream_order_values_1, 
            num_top_stream_order_values_2, 
            selected_rainfall_scenario, 
            crs, 
            qgis_project_name, 
            shp_extract_path, 
            shp_id15_path,
            input_raster_path,
            dtm_raster_path
        )
        messagebox.showinfo("Success", "Analysis completed successfully!")

        # Define path for the QGIS project
        result_directory = os.path.join(input_directory, "Results")
        qgis_project_path = os.path.join(result_directory, f'{qgis_project_name}.qgz')

        # Open QGIS project
        if open_results_var.get() == 1:
            subprocess.run(['qgis', qgis_project_path], check=True)

    except Exception as e:
        print(f"Error: {e}")
        messagebox.showerror("Error", f"An error occurred: {e}")

def start_analysis_thread():
    analysis_thread = threading.Thread(target=run_analysis)
    analysis_thread.start()

# Button to run the analysis
run_button = ctk.CTkButton(root, text="Run Analysis", command=start_analysis_thread, width=150)
run_button.grid(row=8, column=0, columnspan=4, padx=10, pady=20, sticky='ew')

# Function to exit QGIS application
def on_closing():
    qgs.exitQgis()
    root.destroy()

# Bind the closing event to ensure QGIS cleanup
root.protocol("WM_DELETE_WINDOW", on_closing)

# Run the main loop
root.mainloop()
