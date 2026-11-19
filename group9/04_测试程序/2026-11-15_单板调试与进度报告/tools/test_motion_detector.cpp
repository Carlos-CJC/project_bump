#include <cstdio>
#include <cmath>
#include "BumpCore.h"
struct Case { const char* name; int first; int second; float height; int third; int fourth; int expected; bool tilt; };
int main() {
 const Case cases[] = {
  {"quiet",-1,-1,1.0f,-1,-1,0,false},
  {"constant_gravity_rotation",-1,-1,1.0f,-1,-1,0,true},
  {"single_peak",100,-1,2.2f,-1,-1,0,false},
  {"low_amplitude",100,400,1.5f,-1,-1,0,false},
  {"too_fast_100ms",100,200,2.2f,-1,-1,0,false},
  {"too_slow_1000ms",100,1100,2.2f,-1,-1,0,false},
  {"valid_300ms",100,400,2.2f,-1,-1,1,false},
  {"minimum_150ms",100,250,2.2f,-1,-1,1,false},
  {"maximum_800ms",100,900,2.2f,-1,-1,1,false},
  {"cooldown_reject",100,400,2.2f,1000,1300,1,false},
  {"after_cooldown",100,400,2.2f,2800,3100,2,false}
 };
 int failed=0;
 for (const auto& c: cases) {
  bump::ShakeDetector detector; int triggers=0;
  for (int t=0;t<=5000;t+=10) {
   float z=1.0f;
   for(int peak: {c.first,c.second,c.third,c.fourth}) if(peak>=0&&t>=peak&&t<peak+50) z=c.height;
   float x=0;
   if(c.tilt&&t>=1000){x=0.8f;z=0.6f;}
   if(detector.sample(x,0,z,t)) triggers++;
  }
  bool ok=triggers==c.expected; failed+=!ok;
  std::printf("{\"case\":\"%s\",\"source\":\"synthetic_test_vectors\",\"actual_triggers\":%d,\"expected_triggers\":%d,\"pass\":%s}\n",c.name,triggers,c.expected,ok?"true":"false");
 }
 return failed?1:0;
}
