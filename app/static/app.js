function engineColor(e){return e==='A'?'#ff6b7a':e==='B'?'#c278ff':'#43dce8'}
function markColor(kind,e){if(kind==='sell')return '#5ee49b';if(kind==='fail')return '#8c9db3';return engineColor(e)}

function drawKStructure(id,d){
  const el=document.getElementById(id); if(!el)return;
  const c=echarts.init(el);
  const dates=d.bars.map(x=>x.date);
  const vals=d.bars.map(x=>[x.open,x.close,x.low,x.high]);
  const vols=d.bars.map(x=>x.volume);
  const ma10=d.bars.map(x=>x.ma10);
  const ma20=d.bars.map(x=>x.ma20);
  const marks=(d.markers||[]).map(m=>({
    name:m.label,coord:[m.date,m.price],value:m.label,
    symbol:m.kind==='fail'?'circle':m.kind==='sell'?'pin':'pin',symbolSize:m.kind==='fail'?18:42,
    itemStyle:{color:markColor(m.kind,m.engine)},
    label:{formatter:m.label,color:'#fff',fontWeight:800,fontSize:10}
  }));
  const lineData=(d.lines||[]).map(x=>({
    name:x.name,yAxis:x.value,lineStyle:{color:engineColor(x.engine),type:'dashed',opacity:.75,width:1.2},
    label:{formatter:x.name,position:'insideEndTop',color:engineColor(x.engine),fontSize:10}
  }));
  const areaData=(d.areas||[]).map(x=>[
    {name:x.name,xAxis:x.start,yAxis:x.low,itemStyle:{color:engineColor(x.engine)+'18'},label:{show:true,color:engineColor(x.engine),fontSize:9}},
    {xAxis:x.end,yAxis:x.high}
  ]);
  c.setOption({
    animation:false,
    backgroundColor:'transparent',
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#0b1727',borderColor:'#314b68',textStyle:{color:'#eff7ff'}},
    legend:{top:2,textStyle:{color:'#95abc3'},data:['K线','MA10','MA20','成交量']},
    grid:[{left:52,right:20,top:38,height:'66%'},{left:52,right:20,top:'77%',height:'14%'}],
    xAxis:[
      {type:'category',data:dates,boundaryGap:false,axisLine:{lineStyle:{color:'#31465f'}},axisLabel:{color:'#8198b0'}},
      {type:'category',gridIndex:1,data:dates,boundaryGap:false,axisLabel:{show:false},axisLine:{lineStyle:{color:'#31465f'}}}
    ],
    yAxis:[
      {scale:true,splitLine:{lineStyle:{color:'#17283c'}},axisLabel:{color:'#8198b0'}},
      {gridIndex:1,scale:true,splitLine:{show:false},axisLabel:{show:false}}
    ],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:40,end:100},{type:'slider',xAxisIndex:[0,1],bottom:2,height:20,start:40,end:100,borderColor:'#223651',textStyle:{color:'#738aa3'}}],
    series:[
      {name:'K线',type:'candlestick',data:vals,itemStyle:{color:'#e95d68',color0:'#43c990',borderColor:'#e95d68',borderColor0:'#43c990'},markPoint:{data:marks},markLine:{silent:true,symbol:'none',data:lineData},markArea:{silent:true,data:areaData}},
      {name:'MA10',type:'line',data:ma10,showSymbol:false,smooth:false,lineStyle:{width:1.2,color:'#e9edf4'}},
      {name:'MA20',type:'line',data:ma20,showSymbol:false,smooth:false,lineStyle:{width:1.2,color:'#f1c75b'}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vols,itemStyle:{color:'#315477'}}
    ]
  });
  window.addEventListener('resize',()=>c.resize());
}

function drawEquity(id,d){
  const el=document.getElementById(id);if(!el)return;
  const c=echarts.init(el);
  c.setOption({
    animation:false,
    tooltip:{trigger:'axis',backgroundColor:'#0b1727',borderColor:'#314b68',textStyle:{color:'#eff7ff'}},
    grid:[{left:52,right:20,top:28,height:'58%'},{left:52,right:20,top:'75%',height:'15%'}],
    xAxis:[{type:'category',data:d.dates,boundaryGap:false,axisLabel:{color:'#8198b0'}},{type:'category',gridIndex:1,data:d.dates,boundaryGap:false,axisLabel:{show:false}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#17283c'}},axisLabel:{color:'#8198b0'}},{gridIndex:1,min:0,splitLine:{show:false},axisLabel:{color:'#8198b0'}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:0,end:100}],
    series:[
      {name:'账户净值',type:'line',data:d.equity,showSymbol:false,lineStyle:{width:2},areaStyle:{opacity:.08}},
      {name:'持仓数',type:'bar',xAxisIndex:1,yAxisIndex:1,data:d.positions,itemStyle:{opacity:.65}}
    ]
  });
  window.addEventListener('resize',()=>c.resize());
}
