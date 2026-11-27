#!/usr/bin/env python3
"""Compile the actual patched M794 handler against deterministic SdFile/serial stubs.

This exercises firmware code, not a second Python implementation. It cannot prove
real SDIO, MCU timing, electrical, display or actuator compatibility.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zlib

STUB = r'''
#include <iostream>
#include <sstream>
#include <map>
#include <string>
#include <cstring>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <algorithm>
#define ENABLED(x) x
#define NEPTUNE_SD_READ 1
#define SDSUPPORT 1
#define MKS_WIFI_MODULE 0
#define LONG_FILENAME_LENGTH 100
#define O_READ 1
#define ELAPSED(now,when) ((int32_t)((now)-(when))>=0)
using millis_t = uint32_t;
uint32_t clock_ms=0;
uint32_t millis(){return clock_ms;}
bool IsRunning(){return true;}
template<typename... T> void echo(T... args){(std::cout << ... << args);}
template<typename... T> void chars(T... args){(std::cout << ... << char(args));}
#define SERIAL_CHAR(...) chars(__VA_ARGS__)
#define SERIAL_ECHOPGM(...) echo(__VA_ARGS__)
#define SERIAL_ECHOLNPGM(...) do {echo(__VA_ARGS__);std::cout<<std::endl;}while(0)
void native_sd_invalidate(); void native_sd_tick();
std::map<std::string,std::string> files={{"PART.GCO","G90\nM83\nG1 X10 Y0 E1\n"},{"COPY.GCO","G90\nM83\nG1 X10 Y0 E1\n"}};
uint16_t modified=1;
struct dir_t{uint32_t fileSize=0;uint16_t creationDate=1,creationTime=1,lastWriteDate=1,lastWriteTime=1;char name[13]={};};
bool DIR_IS_FILE(dir_t*){return true;}
class SdFile {
 std::string name;uint32_t pos=0, cursor=0;bool opened=false;
public:
 bool open(SdFile*,const char *n,int flags){if(flags!=O_READ)throw 1;name=n;opened=files.count(name);pos=0;return opened;}
 void close(){opened=false;}
 bool isFile(){return opened;}
 bool dirEntry(dir_t *d){if(!opened||!files.count(name))return false;d->fileSize=files[name].size();d->lastWriteTime=modified;return true;}
 uint32_t fileSize(){return opened?files[name].size():0;}
 uint32_t firstCluster(){return name=="PART.GCO"?2:3;}
 bool seekSet(uint32_t p){pos=p;return opened&&pos<=files[name].size();}
 int16_t read(void *out,uint16_t size){if(!opened)return -1;size=std::min<uint32_t>(size,files[name].size()-pos);memcpy(out,files[name].data()+pos,size);pos+=size;return size;}
 void rewind(){cursor=0;}
 int8_t readDir(dir_t *d,char *long_name){if(cursor>=files.size())return 0;auto item=files.begin();std::advance(item,cursor++);strcpy(d->name,item->first.c_str());d->fileSize=item->second.size();strcpy(long_name,item->first.c_str());return 32;}
 static void dirName(const dir_t &d,char *n){strcpy(n,d.name);}
};
struct Card {
 struct {bool saving=false,logging=false;} flag;
 bool mounted=true,printing=false,selected=false;
 bool isMounted(){return mounted;} bool isPrinting(){return printing;} bool isFileOpen(){return selected;}
 SdFile getroot(){return SdFile();}
}card;
struct Timer{bool isRunning(){return card.printing;}bool isPaused(){return false;}}print_job_timer;
struct Planner{bool has_blocks_queued(){return false;}}planner;
struct Queue{struct Ring{struct Port{int index=0;};Port command_port(){return {};}}ring_buffer;}queue;
struct Parser{
 char *command_ptr=nullptr;std::map<char,uint32_t> args;
 uint8_t byteval(char c,uint8_t d=0){return args.count(c)?args[c]:d;}
 uint32_t ulongval(char c){return args[c];}
}parser;
struct GcodeSuite{static void M794();};
'''
MAIN = r'''
int main(){
 std::string line;
 while(std::getline(std::cin,line)){
  if(line=="!remove"){card.mounted=false;native_sd_tick();std::cout<<"fixture"<<std::endl;continue;}
  if(line=="!insert"){card.mounted=true;native_sd_invalidate();std::cout<<"fixture"<<std::endl;continue;}
  if(line=="!modify"){++modified;std::cout<<"fixture"<<std::endl;continue;}
  if(line=="!printing"){card.printing=true;native_sd_tick();std::cout<<"fixture"<<std::endl;continue;}
  if(line=="!idle"){card.printing=false;std::cout<<"fixture"<<std::endl;continue;}
  if(line=="!expire"){clock_ms+=16000;native_sd_tick();std::cout<<"fixture"<<std::endl;continue;}
  parser.args.clear();parser.command_ptr=line.data();
  GcodeSuite::M794();
 }
}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = (args.source / "Marlin/src/gcode/sd/M794.cpp").read_text()
    # Replace only dependencies, retaining actual handlers, guards, CRC and parser.
    source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#include"))
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        raise SystemExit("A native C++17 compiler is required")
    with tempfile.TemporaryDirectory(prefix="rsd-handler-") as directory:
        root = Path(directory)
        crypto_dir = args.source / "Marlin/src/feature/neptune_sd"
        crypto_header = (crypto_dir / "sha256.h").read_text()
        crypto = "\n".join(line for line in (crypto_dir / "sha256.cpp").read_text().splitlines()
                           if not line.lstrip().startswith("#include"))
        (root / "handler.cpp").write_text(STUB + crypto_header + crypto + source + MAIN)
        subprocess.run([compiler, "-std=c++17", "-O1", str(root / "handler.cpp"), "-o", str(root / "handler")], check=True)
        proc = subprocess.Popen([str(root / "handler")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        q = 0
        def exchange(op, session=0, offset=0, count=0, name=""):
            nonlocal q
            q += 1
            wire = f"M794 V1 O{op} Q{q} S{session} A{offset} B{count}"
            if name:
                wire += " P" + "".join(f"{ord(c):03d}" for c in name)
            proc.stdin.write(wire + "\n");proc.stdin.flush()
            reply = proc.stdout.readline()
            assert reply.startswith("RSD1 "), reply
            frame = json.loads(reply[5:]);assert frame["q"] == q
            return frame
        def action(value):
            proc.stdin.write(value+"\n");proc.stdin.flush();assert proc.stdout.readline().strip()=="fixture"
        try:
            assert exchange(0)["chunk"] == 192
            for malformed in ("M794 V257 O0 Q1 S0 A0 B0", "M794 V1 O0 Q1 S0 A0",
                              "M794 V1 O0 Q1 S0 A-1 B0", "M794 V1 O0 Q1 S0 A0 B0 Q2",
                              "M794 V1 O0 Q1 S0 A4294967296 B0"):
                proc.stdin.write(malformed + "\n");proc.stdin.flush()
                assert json.loads(proc.stdout.readline()[5:])["error"]
            assert exchange(1)["name"] == "COPY.GCO"
            assert exchange(1, offset=2)["end"]
            assert "error" in exchange(3, name="../A.GCO")
            opened=exchange(3,name="PART.GCO");session=opened["s"]
            assert "error" in exchange(4,session=session,offset=1,count=1)
            chunk=exchange(4,session=session,count=opened["size"])
            data=bytes.fromhex(chunk["data"])
            assert data==b"G90\nM83\nG1 X10 Y0 E1\n"
            assert chunk["crc"]==zlib.crc32(data)
            assert chunk["token"]==opened["token"]
            assert exchange(5,session=session)["sha256"] == hashlib.sha256(data).hexdigest()
            assert "error" in exchange(4,session=session,count=1)
            for interrupted in ("!modify","!remove","!expire","!printing"):
                opened=exchange(3,name="PART.GCO")
                action(interrupted)
                assert "error" in exchange(4,session=opened["s"],count=1)
                if interrupted=="!remove":action("!insert")
                if interrupted=="!printing":action("!idle")
                # CHANGED closes its handle; all interruptions permit a fresh recovery session.
            opened=exchange(3,name="PART.GCO")
            assert "error" not in exchange(6,session=opened["s"])
            print("Actual M794 handler: capabilities, listing, strict paths, bounded reads, CRC, sessions, close, cancel, modification, media removal, expiry and busy recovery passed (stubbed filesystem).")
        finally:
            proc.stdin.close();proc.wait(timeout=5)


if __name__ == "__main__":
    main()
