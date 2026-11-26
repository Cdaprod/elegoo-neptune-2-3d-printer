import * as T from '/static/vendor/three.module.js';
export function createAirflow(parent,u){
 const geometry=new T.PlaneGeometry(1.3,5);geometry.setAttribute('seed',new T.InstancedBufferAttribute(Float32Array.from({length:256},(_,i)=>(i+.5)/256),1));
 const material=new T.ShaderMaterial({uniforms:{...u,origin:{value:new T.Vector3()}},vertexShader:`attribute float seed;uniform float time,fan;uniform vec3 origin;varying float fade;void main(){float t=fract(seed*7.31+time*(.2+fan*.8));float a=seed*628.3;float r=(4.+t*28.)*(.6+.4*sin(seed*412.));vec3 p=vec3(cos(a)*r,-t*50.,sin(a)*r)+origin;fade=sin(t*3.14159);vec4 view=modelViewMatrix*vec4(p,1.);view.xy+=position.xy;gl_Position=projectionMatrix*view;}`,fragmentShader:`uniform float fan,signal,thermal;varying float fade;void main(){gl_FragColor=vec4(.35,.8,1.,fade*fan*signal*thermal*.4);}`,transparent:true,depthWrite:false,blending:T.AdditiveBlending});
 const mesh=new T.InstancedMesh(geometry,material,256);mesh.frustumCulled=false;parent.add(mesh);
 return {mesh,update(origin,count){material.uniforms.origin.value.copy(origin);mesh.count=count;mesh.visible=u.fan.value>0&&u.signal.value>.01&&u.thermal.value>.01;}};
}
export function createHeatVolume(parent,u){
 const geometry=new T.PlaneGeometry(65,65,8,8);geometry.setAttribute('slice',new T.InstancedBufferAttribute(Float32Array.from({length:20},(_,i)=>(i+.5)/20),1));
 const material=new T.ShaderMaterial({uniforms:{...u,origin:{value:new T.Vector3()}},vertexShader:`attribute float slice;uniform float time,heat;uniform vec3 origin;varying vec2 uv0;varying float height0;void main(){uv0=uv;height0=slice;vec3 p=vec3(position.x*(.6+slice*.8),slice*45.,position.y*(.6+slice*.8));p.x+=sin(slice*8.+time*.25)*slice*heat*3.;p.z+=cos(slice*11.+time*.23)*slice*heat*3.;gl_Position=projectionMatrix*modelViewMatrix*vec4(p+origin,1.);}`,fragmentShader:`varying vec2 uv0;varying float height0;uniform float heat,signal,thermal,time;void main(){vec2 p=(uv0-.5)*2.;float a=exp(-dot(p,p)*3.)*(1.-height0)*heat*signal*thermal*.035;float contour=.75+.25*sin(length(p)*27.-height0*10.+time*.16);gl_FragColor=vec4(1.,.24+height0*.22,.045,a*contour);}`,transparent:true,depthWrite:false,blending:T.AdditiveBlending,side:T.DoubleSide});
 const mesh=new T.InstancedMesh(geometry,material,20);mesh.frustumCulled=false;parent.add(mesh);
 return {mesh,update(origin,count){material.uniforms.origin.value.copy(origin);mesh.count=count;mesh.visible=u.heat.value>0&&u.signal.value>.01&&u.thermal.value>.01;}};
}
