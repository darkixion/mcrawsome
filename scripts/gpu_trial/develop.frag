#version 300 es
precision highp float; precision highp int; precision highp usampler2D; precision highp sampler3D;
uniform usampler2D rawFrame;
uniform sampler2D calibration;
uniform sampler3D lut;
uniform ivec2 rawSize;
uniform ivec2 outputSize;
uniform ivec4 cfa;
uniform mat3 matrix;
uniform int size;
uniform int rotate;
uniform int quad;
uniform float exposure;
out vec4 colour;
float sampleRaw(ivec2 p) {
  if(p.x<0) p.x=-p.x; if(p.y<0) p.y=-p.y;
  if(p.x>=rawSize.x) p.x=2*rawSize.x-2-p.x;
  if(p.y>=rawSize.y) p.y=2*rawSize.y-2-p.y;
  vec2 cal=texelFetch(calibration,p,0).rg;
  return (float(texelFetch(rawFrame,p,0).r)-cal.r)*cal.g;
}
vec3 demosaic(ivec2 p) {
  vec3 rgb=vec3(0);
  if(quad==1) {
    for(int j=0;j<2;j++) for(int i=0;i<2;i++) {
      int c=cfa[j*2+i]; rgb[c]+=sampleRaw(2*p+ivec2(i,j))*(c==1 ? .5 : 1.);
    }
  } else {
    for(int c=0;c<3;c++) {
      float value=0.,weight=0.;
      for(int j=-1;j<=1;j++) for(int i=-1;i<=1;i++) {
        if(cfa[((p.y+j)&1)*2+((p.x+i)&1)]==c) {
          float a=(i==0 ? 2. : 1.)*(j==0 ? 2. : 1.);
          value+=a*sampleRaw(p+ivec2(i,j));weight+=a;
        }
      }
      rgb[c]=value/weight;
    }
  }
  return max(rgb,0.);
}
vec3 at(ivec3 p) { return texelFetch(lut,p,0).rgb; }
vec3 look(vec3 v) {
  vec3 s=clamp(v,0.,1.)*float(size-1);
  ivec3 i=min(ivec3(floor(s)),ivec3(size-2)); vec3 f=s-vec3(i);
  vec3 a=at(i),z=at(i+ivec3(1));
  vec3 r=at(i+ivec3(1,0,0)),g=at(i+ivec3(0,1,0)),b=at(i+ivec3(0,0,1));
  vec3 rg=at(i+ivec3(1,1,0)),rb=at(i+ivec3(1,0,1)),gb=at(i+ivec3(0,1,1));
  if(f.r>f.g) {
    if(f.g>f.b) return a+f.r*(r-a)+f.g*(rg-r)+f.b*(z-rg);
    if(f.r>f.b) return a+f.r*(r-a)+f.b*(rb-r)+f.g*(z-rb);
    return a+f.b*(b-a)+f.r*(rb-b)+f.g*(z-rb);
  }
  if(f.b>f.g) return a+f.b*(b-a)+f.g*(gb-b)+f.r*(z-gb);
  if(f.b>f.r) return a+f.g*(g-a)+f.b*(gb-g)+f.r*(z-gb);
  return a+f.g*(g-a)+f.r*(rg-g)+f.b*(z-rg);
}
void main() {
  ivec2 display=ivec2(gl_FragCoord.xy); display.y=outputSize.y-1-display.y;
  if(rotate==1) display=outputSize-1-display;
  vec2 p=(vec2(display)+.5)*vec2(rawSize)/(vec2(outputSize)*float(quad==1 ? 2 : 1))-.5;
  ivec2 i=ivec2(floor(p));vec2 f=fract(p);
  ivec2 limit=rawSize/(quad==1 ? 2 : 1)-1;
  vec3 camera=mix(mix(demosaic(clamp(i,ivec2(0),limit)),demosaic(clamp(i+ivec2(1,0),ivec2(0),limit)),f.x),
                  mix(demosaic(clamp(i+ivec2(0,1),ivec2(0),limit)),demosaic(clamp(i+ivec2(1),ivec2(0),limit)),f.x),f.y);
  vec3 linear=matrix*camera*exp2(exposure);
  vec3 cct=mix((log2(max(linear,1e-10))+9.72)/17.52,
               linear*10.5402377416545+0.0729055341958355,lessThanEqual(linear,vec3(.0078125)));
  colour=vec4(clamp(look(cct),0.,1.),1.);
}
