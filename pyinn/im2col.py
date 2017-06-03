from pynvrtc.compiler import Program
from torch.autograd import Function
import torch
from torch.nn.modules.utils import _pair
from cupy.cuda.function import Module
from utils import get_compute_arch, Dtype, Stream
from string import Template

CUDA_NUM_THREADS = 1024


def GET_BLOCKS(N):
    return (N + CUDA_NUM_THREADS - 1) // CUDA_NUM_THREADS


def im2col_kernel(**kwargs):
    kernel = '''
#define CUDA_KERNEL_LOOP(i, n)                        \
  for (int i = blockIdx.x * blockDim.x + threadIdx.x; \
      i < (n);                                       \
      i += blockDim.x * gridDim.x)

// Kernel for fast unfold+copy
// (borrowed from Caffe: https://github.com/BVLC/caffe/blob/master/src/caffe/layers/conv_layer.cu)
extern "C"
__global__ void im2col_kernel(const ${Dtype}* data_im, ${Dtype}* data_col) {
  CUDA_KERNEL_LOOP(index, ${n}) {
    int w_out = index % ${width_col};
    index /= ${width_col};
    int h_out = index % ${height_col};
    int channel_in = index / ${height_col};
    int channel_out = channel_in * ${ksize_h} * ${ksize_w};
    int h_in = h_out * ${stride_h} - ${pad_h};
    int w_in = w_out * ${stride_w} - ${pad_w};
    data_col += (channel_out * ${height_col} + h_out) * ${width_col} + w_out;
    data_im += (channel_in * ${height} + h_in) * ${width} + w_in;
    #pragma unroll
    for (int i = 0; i < ${ksize_h}; ++i) {
      for (int j = 0; j < ${ksize_w}; ++j) {
        int h = h_in + i;
        int w = w_in + j;
        *data_col = (h >= 0 && w >= 0 && h < ${height} && w < ${width}) ?
          data_im[i * ${width} + j] : 0;
        data_col += ${height_col} * ${width_col};
      }
    }
  }
}
'''
    return Template(kernel).substitute(**kwargs)


def col2im_kernel(**kwargs):
    kernel = '''
#define CUDA_KERNEL_LOOP(i, n)                        \
  for (int i = blockIdx.x * blockDim.x + threadIdx.x; \
      i < (n);                                       \
      i += blockDim.x * gridDim.x)

extern "C"
__global__ void col2im_kernel(const ${Dtype}* data_col, ${Dtype}* data_im) {
  CUDA_KERNEL_LOOP(index, ${n}) {
    ${Dtype} val = 0;
    int w = index % ${width} + ${pad_w};
    int h = (index / ${width}) % ${height} + ${pad_h};
    int c = index / (${width} * ${height});
    // compute the start and end of the output
    int w_col_start = (w < ${ksize_w}) ? 0 : (w - ${ksize_w}) / ${stride_w} + 1;
    int w_col_end = min(w / ${stride_w} + 1, ${width_col});
    int h_col_start = (h < ${ksize_h}) ? 0 : (h - ${ksize_h}) / ${stride_h} + 1;
    int h_col_end = min(h / ${stride_h} + 1, ${height_col});

    // equivalent implementation
    int offset = (c * ${ksize_h} * ${ksize_w} + h * ${ksize_w} + w) * ${height_col} * ${width_col};
    int coeff_h_col = (1 - ${stride_h} * ${ksize_w} * ${height_col}) * ${width_col};
    int coeff_w_col = (1 - ${stride_w} * ${height_col} * ${width_col});
    #pragma unroll
    for (int h_col = h_col_start; h_col < h_col_end; ++h_col) {
      for (int w_col = w_col_start; w_col < w_col_end; ++w_col) {
        val += data_col[offset + h_col * coeff_h_col + w_col * coeff_w_col];
      }
    }
    data_im[index] = val;
  }
}
    '''
    return Template(kernel).substitute(**kwargs)

im2col_modules = {}

def _im2col(data, kernel_size, stride, padding):
    assert data.dim() == 3
    ksize_h, ksize_w = _pair(kernel_size)
    stride_h, stride_w = _pair(stride)
    pad_h, pad_w = _pair(padding)
    nInputPlane, height, width = data.size()
    height_col = (height + 2 * pad_h - ksize_h) // stride_h + 1
    width_col = (width + 2 * pad_w - ksize_w) // stride_w + 1
    n = nInputPlane * height_col * width_col

    data_col = data.new(nInputPlane, ksize_h, ksize_w, height_col, width_col)

    opt = dict(Dtype=Dtype(data), n=n,
               height_col=height_col,
               width_col=width_col,
               height=height, width=width,
               ksize_h=ksize_h, ksize_w=ksize_w,
               pad_h=pad_h, pad_w=pad_w,
               stride_h=stride_h, stride_w=stride_w,
               channels=nInputPlane)

    kernel_id = hash(frozenset(opt.items()))
    if kernel_id not in im2col_modules:
        kernel = im2col_kernel(**opt)
        print 'Compiling im2col with', opt
        prog = Program(kernel, 'im2col.cu')
        ptx = prog.compile(['-arch='+get_compute_arch(data)])
        module = Module()
        module.load(bytes(ptx.encode()))
        im2col_modules[kernel_id] = module
    else:
        module = im2col_modules[kernel_id]

    f = module.get_function('im2col_kernel')
    f(block=(CUDA_NUM_THREADS,1,1),
      grid=(GET_BLOCKS(n),1,1),
      args=[data.data_ptr(), data_col.data_ptr()],
      stream=Stream(ptr=torch.cuda.current_stream().cuda_stream))
    return data_col


col2im_modules = {}


def _col2im(data_col, kernel_size, stride, padding):
    assert data_col.dim() == 5
    ksize_h, ksize_w = _pair(kernel_size)
    stride_h, stride_w = _pair(stride)
    pad_h, pad_w = _pair(padding)
    nInputPlane, ksize_h, ksize_w, height_col, width_col = data_col.size()
    height = (height_col - 1) * stride_h - 2 * pad_h + ksize_h
    width = (width_col - 1) * stride_w - 2 * pad_w + ksize_w
    n = nInputPlane * height * width

    data = data_col.new(nInputPlane, height, width)

    opt = dict(Dtype=Dtype(data), n=n,
               height_col=height_col,
               width_col=width_col,
               height=height, width=width,
               ksize_h=ksize_h, ksize_w=ksize_w,
               pad_h=pad_h, pad_w=pad_w,
               stride_h=stride_h, stride_w=stride_w,
               channels=nInputPlane)

    kernel_id = hash(frozenset(opt.items()))
    if kernel_id not in col2im_modules:
        kernel = col2im_kernel(**opt)
        print 'Compiling col2im with', opt
        prog = Program(kernel, 'col2im.cu')
        ptx = prog.compile(['-arch='+get_compute_arch(data_col)])
        module = Module()
        module.load(bytes(ptx.encode()))
        col2im_modules[kernel_id] = module
    else:
        module = col2im_modules[kernel_id]

    f = module.get_function('col2im_kernel')
    f(block=(CUDA_NUM_THREADS,1,1),
      grid=(GET_BLOCKS(n),1,1),
      args=[data_col.data_ptr(), data.data_ptr()],
      stream=Stream(ptr=torch.cuda.current_stream().cuda_stream))
    return data


class Im2Col(Function):
    def __init__(self, kernel_size, stride, padding):
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

    def forward(self, input):
        return _im2col(input, self.kernel_size, self.stride, self.padding)

    def backward(self, grad_output):
        return _col2im(grad_output, self.kernel_size, self.stride, self.padding)


class Col2Im(Function):
    def __init__(self, kernel_size, stride, padding):
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

    def forward(self, input):
        return _col2im(input, self.kernel_size, self.stride, self.padding)

    def backward(self, grad_output):
        return _im2col(grad_output, self.kernel_size, self.stride, self.padding)


def im2col(input, kernel_size, stride, padding):
    return Im2Col(kernel_size, stride, padding)(input)


def col2im(input, kernel_size, stride, padding):
    return Col2Im(kernel_size, stride, padding)(input)
