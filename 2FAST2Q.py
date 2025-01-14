import csv
import glob
import os
import gzip
import multiprocessing as mp
import time
import matplotlib.pyplot as plt
import numpy as np
from numba import njit
import psutil
import argparse
import datetime
from tqdm import tqdm
from dataclasses import dataclass
from pathlib import Path
from io import SEEK_END
import zlib

#####################

@dataclass
class Features:
    
    """ Each feature will have its own class instance, where the read counts will be kept.
    Each feature class is stored in a dictionary with the name as its key. 
    See the "guides loader" function """   
    
    name: str
    counts: int

def path_finder(folder_path,extension):
    
    """ Finds the correct file paths from the indicated directories,
    and parses the file names into a list for later use"""
    
    pathing = []
    for exten in extension:
        for filename in glob.glob(os.path.join(folder_path, exten)):
            pathing.append([filename] + [os.path.getsize(filename)]) 
    return pathing

def path_parser(folder_path, extension): 
    
    """ parses the file names and paths into an ordered list for later use"""

    pathing=path_finder(folder_path,extension)
    if extension != '*reads.csv':
        
        """sorting by size makes multiprocessing more efficient 
            as the bigger files will be ran first, thus maximizing processor queing """
        
        ordered = [path[0] for path in sorted(pathing, key=lambda e: e[-1])]#[::-1]

        if ordered == []:
            input(f"Check the path to the {extension[1:]} files folder. No files of this type found.\nPress enter to exit")
            raise Exception

    else:
        ordered = [path[0] for path in sorted(pathing, reverse = False)]

    return ordered

def features_loader(guides):
    
    """ parses the features names and sequences from the indicated features .csv file.
    Creates a dictionary using the feature sequence as key, with an instace of the 
    Features class as respective value. If duplicated features sequences exist, 
    this will be caught in here"""
    
    print("\nLoading Features")
    
    if not os.path.isfile(guides):
        input("\nCheck the path to the sgRNA file.\nNo file found in the following path: {}\nPress enter to exit".format(guides))
        raise Exception
    
    features = {}
    
    try:
        with open(guides) as current: 
            for line in current:
                if "\n" in line:
                    line = line[:-1]
                line = line.split(",")
                sequence = line[1].upper()
                sequence = sequence.replace(" ", "")
                
                if sequence not in features:
                    features[sequence] = Features(line[0], 0)
                    
                else:
                    print(f"\nWarning!!\n{features[sequence].name} and {line[0]} share the same sequence. Only {features[sequence].name} will be considered valid.")

    except IndexError:
        input("\nThe given .csv file doesn't seem to be comma separated. Please double check that the file's column separation is ','\nPress enter to exit")
        raise Exception
        
    print(f"\n{len(features)} different features were provided.")
    return features

def reads_counter(i,o,raw,features,param,cpu,failed_reads,passed_reads,preprocess=False):
    
    """ Reads the fastq file on the fly to avoid RAM issues. 
    Each read is assumed to be composed of 4 lines, with the sequence being 
    on line 2, and the basepair quality on line 4. 
    Every read is trimmed based on the indicated feature positioning. 
    The quality of the obtained trimmed read is crossed against the indicated
    Phred score for quality control.
    If the read has a perfect match with a feature, the respective feature gets a 
    read increase of 1 (done by calling .counts from the respective feature class 
    from the feature class dictionary).
    If the read doesnt have a perfect match, it is sent for mismatch comparison
    via the "imperfect_alignment" function.
    """

    def binary_converter(features):

        """ Parses the input features into numba dictionaries with int8 layout. 
        Converts all DNA sequences to their respective binary array forms. 
        This gives some computing speed advantages with mismatches."""
        
        from numba import types
        from numba.typed import Dict
        
        container = Dict.empty(key_type=types.unicode_type,
                               value_type=types.int8[:])

        for sequence in features:
            container[sequence] = seq2bin(sequence)
        return container
    
    def unfixed_starting_place_parser(read,qual,param):
        
        """ Determines the starting place of a read trimming based on the
        inputed parameters for upstream/downstream sequence matching. 
        Also takes into consideration the quality of that search sequence,
        and mismatches it might have"""

        read_bin=seq2bin(read)
        start,end = None,None

        if (param['upstream'] is not None) & (param['downstream'] is not None):
            start=border_finder(param['upstream'],read_bin,param['miss_search_up'])
            end=border_finder(param['downstream'],read_bin,param['miss_search_down'])
            
            if (start is not None) & (end is not None):
                qual_up = str(qual[start:start+len(param['upstream'])],"utf-8")
                qual_down = str(qual[end:end+len(param['downstream'])],"utf-8")
                
                if (len(param['quality_set_up'].intersection(qual_up)) == 0) &\
                    (len(param['quality_set_down'].intersection(qual_down)) == 0):
                    start+=len(param['upstream'])
                    return start,end

        elif (param['upstream'] is not None) & (param['downstream'] is None):
            start=border_finder(param['upstream'],read_bin,param['miss_search_up'])
            
            if start is not None:
                qual_up = str(qual[start:start+len(param['upstream'])],"utf-8")
                
                if len(param['quality_set_up'].intersection(qual_up)) == 0:
                    start+=len(param['upstream'])
                    end = start + param['length']
                    return start,end
            
        elif (param['upstream'] is None) & (param['downstream'] is not None):
            end=border_finder(param['downstream'],read_bin,param['miss_search_down'])
            
            if end is not None:
                qual_down = str(qual[end:end+len(param['downstream'])],"utf-8")
                
                if len(param['quality_set_down'].intersection(qual_down)) == 0:
                    start = end-param['length']
                    return start,end

        return None,None

    def progress_bar(i,o,raw,cpu):
        
        def getuncompressedsize(raw):
            
            """ Estimates the total size of a .gz compressed 
            file for the progress bars """
            
            def estimate_uncompressed_gz_size(filename):
                with open(filename, "rb") as gz_in:
                    sample = gz_in.read(1000000)
                    gz_in.seek(-4, SEEK_END)
                    file_size = os.fstat(gz_in.fileno()).st_size

                dobj = zlib.decompressobj(31)
                d_sample = dobj.decompress(sample)
            
                compressed_len = len(sample) - len(dobj.unconsumed_tail)
                decompressed_len = len(d_sample)
            
                return int(file_size * decompressed_len / compressed_len)
            
            if ext == ".gz":
                return estimate_uncompressed_gz_size(raw)
            else:
                return os.path.getsize(raw)

        if o > cpu:
            current = mp.current_process()
            pos = current._identity[0]#-1
        else:
            pos = i+1
        total_file_size = getuncompressedsize(raw)
        tqdm_text = f"Processing file {i+1} out of {o}"
        return tqdm(total=total_file_size,desc=tqdm_text, position=pos,colour="green",leave=False,ascii=True,unit="characters")
    
    def fastq_parser(current,end,start,features,failed_reads,passed_reads,fixed_start):
        
        reading = []
        mismatch = [n+1 for n in range(param['miss'])]
        perfect_counter, imperfect_counter, non_aligned_counter, reads,quality_failed = 0,0,0,0,0
        ram_clearance=ram_lock()
        
        if param['miss'] != 0:
            binary_features = binary_converter(features)

        for line in current:
            if (not preprocess) & (param['Progress bar']):
                pbar.update(len(line))
            reading.append(line[:-1])
            
            if len(reading) == 4: #a read always has 4 lines
                
                if not fixed_start:
                    start,end=unfixed_starting_place_parser(str(reading[1],"utf-8"),\
                                                            reading[3],\
                                                            param)
                        
                    if (start is not None) & (end is not None):
                        if end < start: #if the end is not found or found before the start
                            start=None

                if (fixed_start) or (start is not None):
                    seq = str(reading[1][start:end].upper(),"utf-8")
                    quality = str(reading[3][start:end],"utf-8") #convert from bin to str

                    if len(param['quality_set'].intersection(quality)) == 0:
                        
                        if param['Running Mode']=='C':
                            if seq in features:
                                features[seq].counts += 1
                                perfect_counter += 1
                            
                            elif mismatch != []:
                                features,imperfect_counter,failed_reads,passed_reads,non_aligned_counter=\
                                mismatch_search_handler(seq,mismatch,\
                                                        failed_reads,binary_features,\
                                                        imperfect_counter,features,\
                                                        passed_reads,ram_clearance,non_aligned_counter)
                                    
                            else:
                                non_aligned_counter += 1
                        else:
                            if seq not in features:
                                features[seq] = Features(seq, 1)
                            else:
                                features[seq].counts += 1
                            perfect_counter += 1
                            
                    else:
                        quality_failed += 1

                reading = []
                reads += 1
                
                # keeps RAM under control by avoiding overflow
                if reads % 1000000 == 0:
                    ram_clearance=ram_lock()
                
                if preprocess:
                    if reads == 200000:
                        return reads,perfect_counter,imperfect_counter,features,failed_reads,passed_reads
        
        if (not preprocess) & (param['Progress bar']):
            pbar.close()

        return reads,perfect_counter,imperfect_counter,features,failed_reads,passed_reads,non_aligned_counter,quality_failed
    
    fixed_start,end,start = True,0,0
    # determining the read trimming starting/ending place
    if (param['upstream'] is None) & (param['downstream'] is None):
        end = param['start'] + param['length']
        start = param['start']
    else:
        fixed_start = False
        if param['upstream'] is not None:
            param['upstream']=seq2bin(param['upstream'].upper())
        if param['downstream'] is not None:
            param['downstream']=seq2bin(param['downstream'].upper())

    _, ext = os.path.splitext(raw)

    if (not preprocess) & (param['Progress bar']):
        pbar = progress_bar(i,o,raw,cpu)
    
    if ext == ".gz":
        with gzip.open(raw, "rb") as current:
            return fastq_parser(current,end,start,features,failed_reads,passed_reads,fixed_start)
    else:
        with open(raw, "rb")  as current:
            return fastq_parser(current,end,start,features,failed_reads,passed_reads,fixed_start)

def seq2bin(sequence):
    
    """ Converts a string to binary, and then to 
    a numpy array in int8 format"""
    
    sequence = bytearray(sequence,'utf8')
    return np.array((sequence), dtype=np.int8)

@njit
def binary_subtract(array1,array2,mismatch):
    
    """ Used for matching 2 sequences based on the allowed mismatches.
    Requires the sequences to be in numerical form"""
    
    miss=0
    for arr1,arr2 in zip(array1,array2):
        if arr1-arr2 != 0:
            miss += 1
        if miss>mismatch:
            return 0
    return 1

@njit
def border_finder(seq,read,mismatch): 
    
    """ Matches 2 sequences (after converting to int8 format)
    based on the allowed mismatches. Used for sequencing searching
    a start/end place in a read"""
    
    s=seq.size
    r=read.size
    fall_over_index = r-s-1
    for i,bp in enumerate(read): #range doesnt exist in njit
        comparison = read[i:s+i]
        finder = binary_subtract(seq,comparison,mismatch)
        if i > fall_over_index:
            return
        if finder != 0:
            return i

@njit
def features_all_vs_all(binary_features,read,mismatch):
    
    """ Runs the loop of the read vs all sgRNA comparison.
    Sends individually the sgRNAs for comparison.
    Returns the final mismatch score"""
    
    found = 0
    for guide in binary_features:
        if binary_subtract(binary_features[guide],read,mismatch):
            found+=1
            found_guide = guide
            if found>=2:
                return
    if found==1:
        return found_guide
    return #not needed, but here for peace of mind

def mismatch_search_handler(seq,mismatch,failed_reads,binary_features,imperfect_counter,features,passed_reads,ram_clearance,non_aligned_counter):
    
    """Converts a read into numpy int 8 form. Runs the imperfect alignment 
    function for all number of inputed mismatches."""
    
    read=seq2bin(seq)                         
    for miss in mismatch:

        # we already know this read is going to pass
        if seq in passed_reads:
            features[passed_reads[seq]].counts += 1
            imperfect_counter += 1
            break
        
        elif seq not in failed_reads:
            features,imperfect_counter,feature=\
                imperfect_alignment(read,binary_features,\
                                    miss, imperfect_counter,\
                                    features)
            if ram_clearance:
                if feature is None:
                    failed_reads.add(seq)
                    non_aligned_counter += 1
                    break
                else:
                    passed_reads[seq] = feature
        else:
            non_aligned_counter += 1

    return features,imperfect_counter,failed_reads,passed_reads,non_aligned_counter

def imperfect_alignment(read,binary_features, mismatch, counter, features):
    
    """ for the inputed read sequence, this compares if there is a feature 
    with a sequence that is similar to it, to the indicated mismatch degree"""

    feature = features_all_vs_all(binary_features, read, mismatch)
    
    if feature is not None:
        features[feature].counts += 1
        counter += 1

    return features,counter,feature

def aligner(raw,i,o,features,param,cpu,failed_reads,passed_reads):

    """ Runs the main read to sgRNA associating function "reads_counter".
    Creates some visual prompts to alert the user that the samples are being
    processed. Some on the fly quality control is possible (such as making sure 
    the total number of samples is correct, getting an estimate of the total
    number of reads per sample, and checking total running time"""
    
    tempo = time.perf_counter()

    reads, perfect_counter, imperfect_counter, features,failed_reads,passed_reads,non_aligned_counter,quality_failed = \
        reads_counter(i,o,raw,features,param,cpu,failed_reads,passed_reads)

    master_list = []
    [master_list.append([features[guide].name] + [features[guide].counts]) for guide in features]

    tempo = time.perf_counter() - tempo
    if tempo > 3600:
        timing = str(round(tempo / 3600, 2)) + " hours"   
    elif tempo > 60:
        timing = str(round(tempo / 60, 2)) + " minutes"   
    else:
        timing = str(round(tempo, 2)) + " seconds"
    
    name = Path(raw).stem
    path,_ = os.path.splitext(raw)
    if ".fastq" in name:
         name = Path(name).stem
         path,_ = os.path.splitext(path)

    stats_condition = f"#script ran in {timing} for file {name}. {perfect_counter+imperfect_counter} reads out of {reads} were aligned. {perfect_counter} were perfectly aligned. {imperfect_counter} were aligned with mismatch. {non_aligned_counter} passed quality filtering but were not aligned. {quality_failed} did not pass quality filtering."
    
    if not param['Progress bar']:
        print(f"Sample {name} was processed in {timing}")
        
    try:
        master_list.sort(key = lambda master_list: int(master_list[0])) #numerical sorting
    except ValueError:
        master_list.sort(key = lambda master_list: master_list[0]) #alphabetical sorting
    
    master_list.insert(0,["#Feature"] + ["Reads"])
    master_list.insert(0,[stats_condition])
    
    csvfile = os.path.join(param["directory"], name+"_reads.csv")
    csv_writer(csvfile, master_list)
    
    return failed_reads,passed_reads

def csv_writer(path, outfile):
    
    """ writes the indicated outfile into an .csv file in the directory"""
        
    with open(path, "w", newline='') as output: #writes the output
        writer = csv.writer(output)
        writer.writerows(outfile)

def inputs_handler():
    
    """ assertains the correct parsing of the input parameters"""
    
    parameters=inputs_initializer()

    try:
        parameters["start"]=int(parameters["start"])
        parameters["length"]=int(parameters["length"])
        parameters["miss"]=int(parameters["miss"])
        parameters["phred"]=int(parameters["phred"])
        parameters["miss_search_up"]=int(parameters["miss_search_up"])
        parameters["miss_search_down"]=int(parameters["miss_search_down"])
        parameters["qual_up"]=int(parameters["qual_up"])
        parameters["qual_down"]=int(parameters["qual_down"])
    except Exception:
        print("\nPlease confirm you have provided the correct parameters.\nOnly numeric values are accepted in the folowing fields:\n-Feature read starting place;\n-Feature length;\n-mismatch;\n-Phred score.\n")
        exit()
    
    # avoids getting -1 and actually filtering by highest phred score by mistake
    if int(parameters["phred"]) == 0:
        parameters["phred"] = 1
        
    if int(parameters["qual_up"]) == 0:
        parameters["qual_up"] = 1
        
    if int(parameters["qual_down"]) == 0:
        parameters["qual_down"] = 1
    
    if parameters['delete'] == "y":
        parameters['delete'] = True
    else:
        parameters['delete'] = False
        
    if parameters['Progress bar'] == "Yes":
        parameters['Progress bar'] = True
    else:
        parameters['Progress bar'] = False
        
    if parameters['upstream'] == "None":
        parameters['upstream'] = None
        
    if parameters['downstream'] == "None":
        parameters['downstream'] = None
        
    if "Extractor" in parameters['Running Mode']:
        parameters['Running Mode']="EC"
    else:
        parameters['Running Mode']="C"
        
    if parameters['Running Mode']=='C':
        if len(parameters) != 17:
            print("Please confirm that all the input boxes are filled. Some parameters are missing.\nPress enter to exit")
            exit()
            
    parameters["cmd"] = False
    parameters['cpu'] = False

    return parameters

def inputs_initializer():
    
    """ Handles the graphical interface, and all the parameter inputs"""
    
    from tkinter import Entry,LabelFrame,Button,Label,Tk,filedialog,StringVar,OptionMenu
    
    def restart():
        root.quit()
        root.destroy()
        inputs_initializer()
        
    def submit():
        for arg in temporary:
            if ("Use the Browse button to navigate, or paste a link" not in temporary[arg].get()) or\
                ("" not in temporary[arg].get()):
                parameters[arg] = temporary[arg].get()
        root.quit()
        root.destroy()
    
    def directory(column,row,parameter,frame):
        filename = filedialog.askdirectory(title = "Select a folder")
        filing_parser(column,row,filename,parameter,frame)
        
    def file(column,row,parameter,frame):
        filename = filedialog.askopenfilename(title = "Select a file", filetypes = \
            (("CSV files","*.csv"),("all files","*.*")) )
        filing_parser(column,row,filename,parameter,frame)
    
    def filing_parser(column,row,filename,parameter,frame):
        place = Entry(frame,borderwidth=5,width=125)
        place.grid(row=row,column=column+1)
        place.insert(0, filename)
        parameters[parameter] = filename

    def browsing(keyword,inputs,placeholder="Use the Browse button to navigate, or paste a link"):
        title1,title2,row,column,function=inputs
        frame=LabelFrame(root,text=title1,padx=5,pady=5)
        frame.grid(row=row,column=column, columnspan=4)
        button = Button(frame, text = title2,command = lambda: function(column,row,keyword,frame))
        button.grid(column = column, row = row)
        button_place = Entry(frame,borderwidth=5,width=125)
        button_place.grid(row=row,column=column+1, columnspan=4)
        button_place.insert(0, placeholder)
        temporary[keyword]=button_place
        
    def write_menu(keyword,inputs):
        title,row,column,default=inputs
        start = Entry(root,width=25,borderwidth=5)
        start.grid(row=row,column=column+1,padx=20,pady=5)
        start.insert(0, default)
        placeholder(row,column,title,10,1)
        temporary[keyword]=start
        
    def dropdown(keyword,inputs):
        default,option,row,column = inputs
        file_ext = StringVar()
        file_ext.set(default)
        placeholder(row,column,keyword,20,1)
        drop = OptionMenu(root, file_ext, default, option)
        drop.grid(column = column+1,row = row)
        temporary[keyword]=file_ext
        
    def button_click(row, column, title, function):
        button_ok = Button(root,text=title,padx=12,pady=5, width=15,command=function)
        button_ok.grid(row=row, column=column,columnspan=1)
        
    def placeholder(row, column,title,padx,pady):
        placeholder = Label(root, text=title)
        placeholder.grid(row=row,column=column,padx=padx,pady=pady)
        return placeholder
        
    root = Tk()
    root.title("2FAST2Q Input Parameters Window")
    root.minsize(425, 500)
    parameters,temporary = {},{}  

    browsing_inputs = {"seq_files":["Path to the .fastq(.gz) files folder","Browse",1,0,directory],
                       "feature":["Path to the features .csv file","Browse",2,0,file],
                       "out":["Path to the output folder","Browse",3,0,directory]}

    default_inputs = {"out_file_name":["Output File Name",5,0,"Compiled"],
                      "start":["Feature start position in the read",6,0,0],
                      "length":["Feature length",7,0,20],
                      "miss":["Allowed mismatches",8,0,1],
                      "phred":["Minimal feature Phred-score",9,0,30],
                      "delete":["Delete intermediary files [y/n]",10,0,"y"],
                      "upstream":["Upstream search sequence",5,2,"None"],
                      "downstream":["Downstream search sequence",8,2,"None"],
                      "miss_search_up":["Mismatches in the upstream sequence",6,2,0],
                      "miss_search_down":["Mismatches in the downstream sequence",9,2,0],
                      "qual_up":["Minimal upstream sequence Phred-score",7,2,30],
                      "qual_down":["Minimal downstream sequence Phred-score",10,2,30],}
    
    dropdown_options = {"Running Mode":["Counter", "Extractor + Counter",4,0],
                        "Progress bar":["Yes", "No",4,2]}

    # Generating the dropdown browsing buttons
    [dropdown(arg,dropdown_options[arg]) for arg in dropdown_options]
    
    # Generating the file/folder browsing buttons
    [browsing(arg,browsing_inputs[arg]) for arg in browsing_inputs]
    
    # Generating the input parameter buttons
    [write_menu(arg,default_inputs[arg]) for arg in default_inputs]
    
    placeholder(0,1,"",0,0)
    #placeholder(19,0,"",0,0)
    button_click(19, 1, "OK", submit)
    button_click(19, 2, "Reset", restart)

    root.mainloop()

    return parameters

def initializer(cmd):
    
    """ Handles the program initialization process.
    Makes sure the path separators, and the input parser function is correct
    for the used OS.
    Creates the output diretory and handles some parameter parsing"""

    print(f"\nVersion: {version}")

    param = inputs_handler() if cmd is None else cmd

    param["version"] = version
    
    quality_list = '!"#$%&' + "'()*+,-/0123456789:;<=>?@ABCDEFGHI" #Phred score

    param["quality_set"] = set(quality_list[:int(param['phred'])-1])
    param["quality_set_up"] = set(quality_list[:int(param['qual_up'])-1])
    param["quality_set_down"] = set(quality_list[:int(param['qual_down'])-1])
    
    current_time = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
    param["directory"] = os.path.join(param['out'], f"2FAST2Q_output_{current_time}")

    if psutil.virtual_memory().percent>=75:
        print("\nLow RAM availability detected, file processing may be slow\n")
    
    if param['Running Mode']=='C':
        print(f"\nRunning in align and count mode with the following parameters:\n{param['miss']} mismatch allowed\nMinimal Phred Score per bp >= {param['phred']}\nFeature length: {param['length']}\nRead alignment start position: {param['start']}\n")
    else:
        print(f"\nRunning in extract and count mode with the following parameters:\nMinimal Phred Score per bp >= {param['phred']}\n")
        
        if param['upstream'] is not None:
            print(f"Upstream search sequence: {param['upstream']}\n")
            print(f"Mismatches allowed in the upstream search sequence: {param['miss_search_up']}\n")
            print(f"Minimal Phred-score in the upstream search sequence: {param['qual_up']}\n")
            
        if param['downstream'] is not None:
            print(f"Downstream search sequence: {param['downstream']}\n")
            print(f"Mismatches allowed in the downstream search sequence: {param['miss_search_down']}\n")
            print(f"Minimal Phred-score in the downstream search sequence: {param['qual_down']}\n")

        if (param['upstream'] is None) or (param['downstream'] is None):
            print(f"Finding features with the folowing length: {param['length']}bp\n")

    print(f"All data will be saved into {param['directory']}")

    return param

def input_parser():
    
    """ Handles the cmd line interface, and all the parameter inputs"""
    
    global version
    version = "2.5.2"
    
    def current_dir_path_handling(param):
        if param[0] is None:
            parameters[param[1]]=os.getcwd()
            if param[1] == 'feature':
                file = path_finder(os.getcwd(), ["*.csv"])
                if parameters['Running Mode']!="EC":
                    if len(file) > 1:
                        input("There is more than one .csv in the current directory. If not directly indicating a path for sgRNA.csv, please only have 1 .csv file.") 
                        raise Exception
                    if len(file) == 1:
                        parameters[param[1]]=file[0][0]
        else:
            parameters[param[1]]=param[0]
        return parameters
    
    parser = argparse.ArgumentParser()
    parser.add_argument("-c",nargs='?',const=True,help="cmd line mode")
    parser.add_argument("-v",nargs='?',const=True,help="prints the installed version")
    parser.add_argument("--s",help="The full path to the directory with the sequencing files OR file")
    parser.add_argument("--g",help="The full path to the .csv file with the sgRNAs.")
    parser.add_argument("--o",help="The full path to the output directory")
    parser.add_argument("--fn",nargs='?',const="compiled",help="Specify an output compiled file name (default is called compiled)")
    parser.add_argument("--pb",nargs='?',const=False,help="Adds progress bars (default is enabled)")
    parser.add_argument("--m",help="number of allowed mismatches (default=1)")
    parser.add_argument("--ph",help="Minimal Phred-score (default=30)")
    parser.add_argument("--st",help="Feauture start position in the read (default is 0==1st bp)")
    parser.add_argument("--l",help="Feature length (default=20bp)")
    parser.add_argument("--us",help="Upstream search sequence")
    parser.add_argument("--ds",help="Downstream search sequence")
    parser.add_argument("--msu",help="mismatches allowed in the upstream sequence")
    parser.add_argument("--msd",help="mismatches allowed in the downstream sequence")
    parser.add_argument("--qsu",help="Minimal Phred-score (default=30) in the upstream search sequence")
    parser.add_argument("--qsd",help="Minimal Phred-score (default=30) in the downstream search sequence")
    parser.add_argument("--mo",help="Running Mode (default=C) [Counter (C) / Extractor + Counter (EC)]")
    parser.add_argument("--cp",help="Number of cpus to be used (default is max(cpu)-2 for >=3 cpus, -1 for >=2 cpus, 1 if 1 cpu")
    parser.add_argument("--k",nargs='?',const=False,help="If enabled, keeps all temporary files (default is disabled)")
    args = parser.parse_args()
    
    if args.v is not None:
        print(f"\nVersion: {version}\n")
        exit()
    
    #if its not running on command window mode
    if args.c is None:
        return None
    
    parameters = {}
    parameters["cmd"] = True
    paths_param = [[args.s,'seq_files'],
                   [args.g,'feature'],
                   [args.o,'out']]
    
    parameters['out_file_name'] = "compiled"
    if args.fn is not None:
        parameters['out_file_name'] = args.fn

    parameters['length']=20
    if args.l is not None:
        parameters['length']=int(args.l)
        
    parameters['Progress bar']=True
    if args.pb is not None:
        parameters['Progress bar']=False
                 
    parameters['start']=0
    if args.st is not None:
        parameters['start']=int(args.st)
        
    parameters['phred']=30
    if args.ph is not None:
        parameters['phred']=int(args.ph)
        if int(parameters["phred"]) == 0:
            parameters["phred"] = 1
                 
    parameters['miss']=1
    if args.m is not None:
        parameters['miss']=int(args.m)
        
    parameters['upstream']=None
    if args.us is not None:
        parameters['upstream']=args.us
        
    parameters['downstream']=None
    if args.ds is not None:
        parameters['downstream']=args.ds
                 
    parameters['miss_search_up']=0
    if args.msu is not None:
        parameters['miss_search_up']=int(args.msu)
        
    parameters['miss_search_down']=0
    if args.msd is not None:
        parameters['miss_search_down']=int(args.msd)
        
    parameters['qual_up']=30
    if args.qsu is not None:
        parameters['qual_up']=int(args.qsu)
        
    parameters['qual_down']=30
    if args.qsd is not None:
        parameters['qual_down']=int(args.qsd)
        
    parameters['Running Mode']="C"
    if args.mo is not None:
        if "EC" in args.mo.upper():
            parameters['Running Mode']="EC"
        
    parameters['delete']=True
    if args.k is not None:
        parameters['delete']=False
        
    parameters['cpu']=False
    if args.cp is not None:
        parameters['cpu']=int(args.cp)
        
    for param in paths_param:
        parameters = current_dir_path_handling(param)
        
    return parameters

def compiling(param):

    """ Combines all the individual processed .csv files into one final file.
    Gathers the individual sample statistic and parses it into "run_stats" """
    
    ordered_csv = path_parser(param["directory"], ['*reads.csv'])

    headers = [f"#2FAST2Q version: {param['version']}"] + \
            [f"#Mismatch: {param['miss']}"] + \
            [f"#Phred Score: {param['phred']}"] + \
            [f"#Feature Length: {param['length']}"] + \
            [f"#Feature start position in the read: {param['start']}"] + \
            [f"#Running mode: {param['Running Mode']}"] + \
            [f"#Upstream search sequence: {param['upstream']}"] + \
            [f"#Downstream search sequence: {param['downstream']}"] + \
            [f"#Mismatches in the upstream search sequence: {param['miss_search_up']}"] + \
            [f"#Mismatches in the downstream search sequence: {param['miss_search_down']}"] + \
            [f"#Minimal Phred-score in the upstream search sequence: {param['qual_up']}"] + \
            [f"#Minimal Phred-score in the downstream search sequence: {param['qual_down']}"]
            
    headers = headers[::-1]

    compiled = {} #dictionary with all the reads per feature
    head = ["#Feature"] #name of the samples
    for i, file in enumerate(ordered_csv):
        path,_ = os.path.splitext(file)
        path = Path(path).stem
        path = path[:-len("_reads")]
        head.append(path)
        with open(file) as current:
            for line in current:
                line = line[:-1].split(",")
                if "#" not in line[0]:
                    
                    if line[0] in compiled: 
                        compiled[line[0]] = compiled[line[0]] + [int(line[1])]
                    else:
                        compiled[line[0]] = [0]*i + [int(line[1])]
                        
                elif "#Feature" not in line[0]:
                    headers.append(line[0][1:]+"\n")
    
        #important in extract and count mode for creating entries with 0 reads
        for entry in compiled:
            if len(compiled[entry])<i+1:
                compiled[entry] = compiled[entry] + [0]*(i+1-len(compiled[entry]))

    run_stats(headers,param,compiled,head)

    final = []
    [final.append([feature] + compiled[feature]) for feature in compiled] 
    final.insert(0, head)
    
    csvfile = os.path.join(param["directory"],f"{param['out_file_name']}.csv")
    csv_writer(csvfile, final)

    if param["delete"]:
        for file in ordered_csv:
            os.remove(file)

    print("\nIf you find 2FAST2Q useful, please consider citing:\nBravo AM, Typas A, Veening J. 2022. \n2FAST2Q: a general-purpose sequence search and counting program for FASTQ files. PeerJ 10:e14041\nDOI: 10.7717/peerj.14041\n")
    
    if not param["cmd"]:
        input(f"\nAnalysis successfully completed\nAll the reads have been saved into {csvfile}.\nPress enter to exit")

def run_stats(headers, param, compiled, head):
    
    """ Manipulates the statistics from all the samples into one file that can
    be used for downstream user quality control aplications. Creates a simple
    bar graph with the number of reads per sample"""
    
    ### parsing the stats from the read files
    global_stat = [["#Sample name", "Running Time", "Running Time unit", \
                    "Total number of reads in sample", \
                    "Total number of reads that were aligned", \
                    "Number of reads that were aligned without mismatches", \
                    "Number of reads that were aligned with mismatches",\
                    "Number of reads that passed quality filtering but were not aligned",\
                    'Number of reads that did not pass quality filtering.']]
    
    header_ofset = 1
    for run in headers:
        if "script ran" in run:
            parsed = run.split()
            global_stat.append([parsed[7][:-1]] + [parsed[3]] + [parsed[4]] + \
                               [parsed[12]] + [parsed[8]] + [parsed[15]] + \
                               [parsed[19]] + [parsed[24]] + [parsed[32]])
        else:
            global_stat.insert(0,[run])
            header_ofset+=1
            
    csvfile = os.path.join(param["directory"],f"{param['out_file_name']}_stats.csv")
    csv_writer(csvfile, global_stat)
    
    ######## for bar plots with absolute number of reads
    
    fig, ax = plt.subplots(figsize=(12, int(len(global_stat)/4)))
    width = .75
    for i, (_,_,_,total_reads,aligned,_,_,not_aligned,_) in enumerate(global_stat[header_ofset:]):   
        
        plt.barh(i, int(total_reads), width,  capsize=5, color = "#FFD25A", hatch="//",edgecolor = "black", linewidth = .7)
        plt.barh(i, int(aligned), width,  capsize=5, color = "#FFAA5A", hatch="\\",edgecolor = "black", linewidth = .7)
        plt.barh(i, int(not_aligned), width, capsize=5, color = "#F56416", hatch="x",edgecolor = "black", linewidth = .7)
    
    ax.set_yticks(np.arange(len([n[0] for n in global_stat[header_ofset:]])))
    ax.set_yticklabels([n[0] for n in global_stat[header_ofset:]])
    ax.tick_params(axis='both', which='major', labelsize=16)
    ax.tick_params(axis='both', which='minor', labelsize=16)
    plt.xlabel('Number of reads',size=20)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    #ax.set_xscale('log')
    ax.set_xlim(xmin=1)
    ax.legend(["Total reads in sample", "Aligned reads","Reads that passed quality filtering but failed to align"], \
              loc='right',bbox_to_anchor=(1.1, 1),ncol=3,prop={'size': 12})
    plt.tight_layout()
    file = os.path.join(param["directory"],f"{param['out_file_name']}_reads_plot.png")
    plt.savefig(file, dpi=300, bbox_inches='tight')
    
    ######## for bar plots with relative (percentage) number of reads
    
    fig, ax = plt.subplots(figsize=(12, int(len(global_stat)/4)))
    width = .75
    for i, (_,_,_,total_reads,aligned,_,_,not_aligned,q_failed) in enumerate(global_stat[header_ofset:]):   

        aligned = int(aligned)/int(total_reads)*100
        not_aligned = int(not_aligned)/int(total_reads)*100
        q_failed = int(q_failed)/int(total_reads)*100
        
        plt.barh(i, aligned, width,  capsize=5, color = "#6290C3", hatch="\\",edgecolor = "black", linewidth = .7)
        plt.barh(i, not_aligned, width, capsize=5, left=aligned, color = "#F1FFE7", hatch="//",edgecolor = "black", linewidth = .7)
        plt.barh(i, q_failed, width, capsize=5, left=not_aligned+aligned, color = "#FB5012", hatch="||",edgecolor = "black", linewidth = .7)
    
    ax.set_yticks(np.arange(len([n[0] for n in global_stat[header_ofset:]])))
    ax.set_yticklabels([n[0] for n in global_stat[header_ofset:]])
    ax.tick_params(axis='both', which='major', labelsize=16)
    ax.tick_params(axis='both', which='minor', labelsize=16)
    plt.xlabel('% of reads per sample',size=20)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    #ax.set_xscale('log')
    ax.set_xlim(xmin=1)
    ax.legend(["Aligned reads","Reads that passed quality filtering but failed to align","Reads that did not pass quality filtering"], \
              loc='right',bbox_to_anchor=(1.1, 1),ncol=3,prop={'size': 12})
    plt.tight_layout()
    file = os.path.join(param["directory"],f"{param['out_file_name']}_reads_plot_percentage.png")
    plt.savefig(file, dpi=300, bbox_inches='tight')
    
    ########
    """ For plotting a violin plot distribution """
    
    distributions = {}
    for feature in compiled:
        for i,read in enumerate(compiled[feature]):
            if head[i+1] in distributions:
                distributions[head[i+1]].append(read)
            else:
                distributions[head[i+1]] = [read]
    
    def adjacent_values(vals, q1, q3):
        upper_adjacent_value = q3 + (q3 - q1) * 1.5
        upper_adjacent_value = np.clip(upper_adjacent_value, q3, vals[-1])
    
        lower_adjacent_value = q1 - (q3 - q1) * 1.5
        lower_adjacent_value = np.clip(lower_adjacent_value, vals[0], q1)
        return lower_adjacent_value, upper_adjacent_value

    def set_axis_style(ax, labels):
        ax.xaxis.set_tick_params(direction='out')
        ax.xaxis.set_ticks_position('bottom')
        ax.set_xticks(np.arange(1, len(labels) + 1), labels=labels)
        ax.set_xlim(0.25, len(labels) + 0.75)
        ax.set_xlabel('Sample name')
        
    def violin(data,head,normalized=False):
        fig, ax = plt.subplots(figsize=(12, int(len(global_stat))/2))
        if not normalized:
            ax.set_title('Reads per feature distribution',size=20)
        else:
            ax.set_title('Reads per feature (RPM normalized) distribution',size=20)
        plt.xlabel('Reads per feature',size=20)
        parts=ax.violinplot(data, points=200, widths=1, showmeans=False, showmedians=False,showextrema=False,vert=False)
        
        for pc in parts['bodies']:
            pc.set_facecolor('#D43F3A')
            pc.set_edgecolor('black')
            pc.set_alpha(1)
        
        quartile1, medians, quartile3 = np.percentile(data, [25, 50, 75], axis=1)
        inds = np.arange(1, len(medians) + 1)
        ax.scatter(medians,inds, marker='o', color='white', s=40, zorder=3)
        ax.hlines(inds,quartile1, quartile3, color='k', linestyle='-', lw=8)
        #ax.hlines(inds,whiskers_min, whiskers_max, color='k', linestyle='-', lw=2.5)
        ax.set_yticks(np.arange(len(head[1:]))+1)
        ax.set_yticklabels(head[1:])
        plt.tick_params(axis='y', which='major', labelsize=20)
        plt.tick_params(axis='x', which='major', labelsize=20)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        #ax.set_xscale('log')
        ax.set_xlim(xmin=1)
        if not normalized:
            file = os.path.join(param["directory"],f"{param['out_file_name']}_distribution_plot.png")
        else:
            file = os.path.join(param["directory"],f"{param['out_file_name']}_distribution_normalized_RPM_plot.png")
        plt.savefig(file, dpi=300, bbox_inches='tight')
    
    data = []
    for entry in distributions:
        data.append(distributions[entry])
    violin(data,head)
    
    ## normalized RPM
    try:
        data = np.array(data)
        data1 = []
        for i,entry in enumerate(data):
            if sum(entry)>0:
                data1.append(entry/sum(entry)*1000000) #RPM
        violin(data1,head,normalized=True)
    except ValueError:
        pass

def ram_lock():

    """ stops increasing the hash tables (failed and passed reads) 
    size to avoid using RAM that isnt there """

    if psutil.virtual_memory().percent >= 95:
        return False
    return True

def cpu_counter(cpu):
    
    """ counts the available cpu cores, required for spliting the processing
    of the files """
    
    available_cpu = mp.cpu_count()
    if type(cpu) is not int:
        cpu = available_cpu
        if cpu >= 3:
            cpu -= 2
        if cpu == 2:
            cpu -= 1
    else:
        if cpu > available_cpu:
            cpu = available_cpu

    pool = mp.Pool(processes = cpu, 
                    initargs=(mp.RLock(),), 
                    initializer=tqdm.set_lock)
    
    return pool,cpu

def multiprocess_merger(start,failed_reads,passed_reads,files,features,param,pool,cpu,preprocess=False):
    
    """ runs all samples in blocks equal to the amount of cpus in the PC. 
    Before starting a new block, the hash tables for the failed and passed reads 
    are updated. This confers speed advantages for the next block. """ 
    
    result = []
    for i, name in enumerate(files[start:start+cpu]):
        
        result.append(pool.apply_async(aligner, args=(name,\
                                                      i+start,\
                                                      len(files),\
                                                      features,\
                                                      param,\
                                                      cpu,\
                                                      failed_reads,\
                                                      passed_reads)))
    
    if param["miss"] != 0:
        compiled = [x.get() for x in result]
        failed_reads_compiled,passed_reads_compiled = zip(*compiled)
        return hash_reads_parsing(result,failed_reads_compiled,passed_reads_compiled,failed_reads,passed_reads)
    
    else:
        return failed_reads,passed_reads

def hash_reads_parsing(result,failed_reads_compiled,passed_reads_compiled,failed_reads,passed_reads):
    
    """ parsed the results from all the processes, merging the individual failed and
    passed reads into one master file, that will be subsquently used for the new
    samples's processing """ 
    
    for failed,passed in zip(failed_reads_compiled,passed_reads_compiled):
        failed_reads = set.union(failed_reads,failed)
        passed_reads = {**passed_reads,**passed}
        
    return failed_reads,passed_reads

def hash_preprocesser(files,features,param,pool,cpu):
    
    """ For the smallest files, we processe them for the first x amount of reads to
    initialize the failed reads and passed reads hash tables. This will confer some 
    speed advantages, as subsquent files normally share the same reads that dont
    align to anything, or reads with mismatches that indeed align"""
    
    print("\nPlease standby for the initialization procedure.")
    result=[]
    for name in files[:cpu]:
        result.append(pool.apply_async(reads_counter, args=(0,0,name,features,param,cpu,set(),{},True)))
    
    compiled = [x.get() for x in result]
    throw1,throw2,throw3,throw4,failed_reads_compiled,passed_reads_compiled = zip(*compiled)
    
    return hash_reads_parsing(result,failed_reads_compiled,passed_reads_compiled,set(),{})

def aligner_mp_dispenser(files,features,param):
    
    """ starts and handles the parallel processing of all the samples by calling 
    multiple instances of the "aligner" function (one per sample) """
    
    if not os.path.exists(param["directory"]):
        os.makedirs(param["directory"])
    
    start,failed_reads,passed_reads = 0,set(),{}
    pool,cpu = cpu_counter(param["cpu"])
    
    if (len(files)>cpu) & (param["miss"] != 0):
        failed_reads,passed_reads=hash_preprocesser(files,features,param,pool,cpu)
    
    print(f"\nProcessing {len(files)} files. Please hold.")
    for start in range(0,len(files),cpu):
        failed_reads,passed_reads = \
        multiprocess_merger(start,failed_reads,passed_reads,files,\
                            features,param,pool,cpu)
        
    pool.close()
    pool.join()

def main():
    
    """ Runs the program by calling all the appropriate functions"""
    
    ### parses all inputted parameters
    param = initializer(input_parser())
    
    ### parses the names/paths, and orders the sequencing files
    files = [param["seq_files"]]
    if os.path.splitext(param["seq_files"])[1] == '':
        files = path_parser(param["seq_files"], ["*.gz","*.fastq"])

    ### loads the features from the input .csv file. 
    ### Creates a dictionary "feature" of class instances for each sgRNA
    features = {}
    if param['Running Mode']=='C':
        features = features_loader(param["feature"])
    
    ### Processes all the samples by associating sgRNAs to the reads on the fastq files.
    ### Creates one process per sample, allowing multiple samples to be processed in parallel. 
    aligner_mp_dispenser(files,features,param)
    
    ### Compiles all the processed samples from multi into one file, and creates the run statistics
    compiling(param)
    
if __name__ == "__main__":
    mp.freeze_support() # required to run multiprocess as .exe on windows
    main()
