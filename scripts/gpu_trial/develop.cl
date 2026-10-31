// Trial only: bilinear or Bayer quads, calibration, AP1, exposure, ACEScct and tetrahedral look.
float sample(__global const ushort *raw, __global const float *black,
             __global const float *gain, int x, int y, int w, int h) {
    // Reflect by two sites at the edge so the CFA phase does not change.
    x = x < 0 ? -x : (x >= w ? 2*w-2-x : x);
    y = y < 0 ? -y : (y >= h ? 2*h-2-y : y);
    int i = y*w+x;
    return ((float)raw[i]-black[i])*gain[i];
}
__kernel void develop(__global const ushort *raw, __global const float *black,
                      __global const float *gain, __global const int *cfa,
                      __global const float *matrix, __global float *linear,
                      int w, int h, int quad) {
    int x=get_global_id(0), y=get_global_id(1), dw=quad ? w/2 : w;
    float3 rgb=(float3)(0);
    if (quad) {
        for (int j=0;j<2;j++) for(int i=0;i<2;i++) {
            int c=cfa[j*2+i];
            rgb[c] += sample(raw,black,gain,2*x+i,2*y+j,w,h)*(c==1 ? 0.5f : 1.0f);
        }
    } else {
        for (int c=0;c<3;c++) {
            float value=0, weight=0;
            for(int j=-1;j<=1;j++) for(int i=-1;i<=1;i++) {
                if (cfa[((y+j)&1)*2+((x+i)&1)]==c) {
                    float a=(i==0 ? 2.0f : 1.0f)*(j==0 ? 2.0f : 1.0f);
                    value+=a*sample(raw,black,gain,x+i,y+j,w,h); weight+=a;
                }
            }
            rgb[c]=value/weight;
        }
    }
    rgb=fmax(rgb,0.0f);
    float3 ap1=(float3)(dot(rgb,vload3(0,matrix)),dot(rgb,vload3(1,matrix)),dot(rgb,vload3(2,matrix)));
    vstore3(ap1,y*dw+x,linear);
}
float3 at(__global const float *table, int3 p, int n) {
    return vload3((p.z*n+p.y)*n+p.x,table);
}
float3 look(__global const float *table, float3 v, int n) {
    float3 s=clamp(v,0.0f,1.0f)*(n-1);
    int3 i=min(convert_int3(floor(s)),(int3)(n-2)); float3 f=s-convert_float3(i);
    float3 a=at(table,i,n), z=at(table,i+(int3)(1),n);
    float3 r=at(table,i+(int3)(1,0,0),n), g=at(table,i+(int3)(0,1,0),n);
    float3 b=at(table,i+(int3)(0,0,1),n), rg=at(table,i+(int3)(1,1,0),n);
    float3 rb=at(table,i+(int3)(1,0,1),n), gb=at(table,i+(int3)(0,1,1),n);
    if (f.x>f.y) {
        if(f.y>f.z) return a+f.x*(r-a)+f.y*(rg-r)+f.z*(z-rg);
        if(f.x>f.z) return a+f.x*(r-a)+f.z*(rb-r)+f.y*(z-rb);
        return a+f.z*(b-a)+f.x*(rb-b)+f.y*(z-rb);
    }
    if(f.z>f.y) return a+f.z*(b-a)+f.y*(gb-b)+f.x*(z-gb);
    if(f.z>f.x) return a+f.y*(g-a)+f.z*(gb-g)+f.x*(z-gb);
    return a+f.y*(g-a)+f.x*(rg-g)+f.z*(z-rg);
}
__kernel void finish(__global const float *linear, __global const float *table,
                     __global uchar *output, int w, int h, int ow, int oh,
                     int n, int rotate, float exposure) {
    int x=get_global_id(0), y=get_global_id(1);
    // Exact box weights when shrinking. The trial never enlarges the compute output.
    float left=(float)x*w/ow, right=(float)(x+1)*w/ow;
    float top=(float)y*h/oh, bottom=(float)(y+1)*h/oh;
    float3 rgb=(float3)(0); float total=0;
    for(int j=(int)floor(top);j<(int)ceil(bottom);j++)
      for(int i=(int)floor(left);i<(int)ceil(right);i++) {
        float weight=(fmin(right,i+1.0f)-fmax(left,(float)i))*(fmin(bottom,j+1.0f)-fmax(top,(float)j));
        int sx=rotate ? w-1-i : i, sy=rotate ? h-1-j : j;
        rgb+=vload3(sy*w+sx,linear)*weight; total+=weight;
      }
    rgb=rgb/total*exp2(exposure);
    float3 cct=select((log2(fmax(rgb,1e-10f))+9.72f)/17.52f,
                     rgb*10.5402377416545f+0.0729055341958355f,rgb<=0.0078125f);
    float3 result=clamp(look(table,cct,n),0.0f,1.0f);
    vstore3(convert_uchar3_sat(floor(result*255.0f+0.5f)),y*ow+x,output);
}
